#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地生成并可选提交“语义相关性正式标注”项目的 AI 辅助结果。

默认只读取项目导出 JSON、生成预览计划，不访问网站，也不会提交。
只有同时传入 --submit 和 --acknowledge-ai-assisted 时才会登录并提交。
密码从隐藏输入或环境变量 FOLK_LABEL_PASSWORD 读取。
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any


DEFAULT_PASSWORD_ENV = "FOLK_LABEL_PASSWORD"
# 整体备注保持为空；单个 Query 的答案仍由 answers 保存。
AI_NOTE = ""


class ValidationError(RuntimeError):
    """输入数据与预期项目结构不一致。"""


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"{method} {path}: HTTP {status} {detail}")
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail


REQUIRED_ROUTES = {"login", "session", "project", "project_export", "item_detail", "submit_item"}


def load_routes(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(
            f"找不到路由配置 {path}；先复制 examples/api_routes.example.json 并按授权服务配置"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"路由配置不是有效 JSON：{path}") from exc
    if not isinstance(data, dict):
        raise ValidationError("路由配置顶层必须是对象")
    missing = REQUIRED_ROUTES.difference(data)
    if missing:
        raise ValidationError(f"路由配置缺少操作：{', '.join(sorted(missing))}")
    empty = [key for key in REQUIRED_ROUTES if not isinstance(data.get(key), str) or not data[key].strip()]
    if empty:
        raise ValidationError(f"请先填写本地路由配置：{', '.join(sorted(empty))}")
    return {key: str(value) for key, value in data.items()}


class ApiClient:
    def __init__(self, base_url: str, routes: dict[str, str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.routes = routes
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=90) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ApiError(method, path, exc.code, detail) from exc
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                TimeoutError,
                socket.timeout,
                ConnectionError,
            ) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        raise RuntimeError(f"{method} {path}: 网络连接失败：{last_error}")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any | None = None) -> Any:
        return self.request("POST", path, payload if payload is not None else {})

    def route(self, name: str, **params: Any) -> str:
        template = self.routes.get(name)
        if not isinstance(template, str) or not template.strip():
            raise ValidationError(f"路由配置缺少 {name}")
        encoded = {key: urllib.parse.quote(str(value), safe="") for key, value in params.items()}
        try:
            path = template.format(**encoded)
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"路由 {name} 模板无效") from exc
        if not path.startswith("/") or "{" in path or "}" in path:
            raise ValidationError(f"路由 {name} 必须是以 / 开头的相对路径模板")
        return path

    def get_route(self, name: str, **params: Any) -> Any:
        return self.get(self.route(name, **params))

    def post_route(self, name: str, payload: Any | None = None, **params: Any) -> Any:
        return self.post(self.route(name, **params), payload)

    def login(self, email: str, password: str) -> dict[str, Any]:
        data = self.post_route("login", {"email": email, "password": password})
        user = data.get("user") or {}
        if not user:
            raise RuntimeError("登录失败：接口未返回 user")
        auth = self.get_route("session")
        if not auth.get("authenticated"):
            raise RuntimeError("登录失败：Cookie 未生效")
        return user


@dataclass(frozen=True)
class PairPlan:
    pair_id: str
    description: str
    grade: int
    source_grades: list[int]
    agreement: float

    def answer(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "relevance_grade": self.grade,
            "unable_to_judge": False,
            "reason": "",
        }


@dataclass(frozen=True)
class ItemPlan:
    item_id: int
    item_key: str
    title: str
    lyrics: str
    assignment_id: int
    pairs: list[PairPlan]

    def payload(self) -> dict[str, Any]:
        return {"answers": [pair.answer() for pair in self.pairs], "note": AI_NOTE}


@dataclass
class ActionResult:
    item_id: int
    title: str
    action: str
    detail: str


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def latest_default_export() -> Path | None:
    downloads = Path.home() / "Downloads"
    matches = list(downloads.glob("semantic_relevance_export_*.json"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"找不到导出文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"导出文件不是有效 JSON：{path}：{exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("导出 JSON 顶层必须是对象")
    return data


def parse_grade(value: Any, pair_id: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"pair {pair_id} 的 llm_grade_final 不是 0–3 整数")
    try:
        grade = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"pair {pair_id} 缺少有效 llm_grade_final") from exc
    if grade not in {0, 1, 2, 3}:
        raise ValidationError(f"pair {pair_id} 的等级越界：{grade}")
    return grade


def parse_source_grades(value: Any) -> list[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    output: list[int] = []
    for raw in value:
        if isinstance(raw, bool):
            continue
        try:
            grade = int(raw)
        except (TypeError, ValueError):
            continue
        if grade in {0, 1, 2, 3}:
            output.append(grade)
    return output


def find_own_assignment(item: dict[str, Any], email: str) -> dict[str, Any] | None:
    for assignment in item.get("assignments") or []:
        if str(assignment.get("annotator_email") or "").casefold() == email.casefold():
            return assignment
    return None


def build_item_plan(
    item: dict[str, Any], email: str, include_submitted: bool = False
) -> ItemPlan | None:
    assignment = find_own_assignment(item, email)
    if not assignment or (
        assignment.get("status") == "submitted" and not include_submitted
    ):
        return None

    item_meta = item.get("item") or {}
    semantic = item.get("semantic_context") or {}
    song = item.get("song") or {}
    item_id = int(item_meta.get("id") or 0)
    assignment_id = int(assignment.get("id") or 0)
    if not item_id or not assignment_id:
        raise ValidationError("待标任务缺少 item_id 或 assignment_id")

    pairs: list[PairPlan] = []
    for pair in semantic.get("pairs") or []:
        pair_id = str(pair.get("pair_id") or "").strip()
        if not pair_id:
            raise ValidationError(f"item #{item_id} 存在缺少 pair_id 的问题")
        grade = parse_grade(pair.get("llm_grade_final"), pair_id)
        source_grades = parse_source_grades(pair.get("llm_grades"))
        agreement = (
            sum(candidate == grade for candidate in source_grades) / len(source_grades)
            if source_grades
            else 0.0
        )
        pairs.append(
            PairPlan(
                pair_id=pair_id,
                description=str(pair.get("description") or ""),
                grade=grade,
                source_grades=source_grades,
                agreement=agreement,
            )
        )
    if not pairs:
        raise ValidationError(f"item #{item_id} 没有语义相关性 Query")

    return ItemPlan(
        item_id=item_id,
        item_key=str(item_meta.get("item_key") or ""),
        title=str(song.get("title") or semantic.get("song_title") or item_id),
        lyrics=str(semantic.get("lyrics") or ""),
        assignment_id=assignment_id,
        pairs=pairs,
    )


def build_plan(
    export_data: dict[str, Any],
    email: str,
    project_id: int,
    selected_item_ids: set[int],
    min_agreement: float,
    include_submitted: bool = False,
) -> tuple[list[ItemPlan], list[ItemPlan]]:
    project = export_data.get("project") or {}
    actual_project_id = int(project.get("id") or 0)
    if actual_project_id != project_id:
        raise ValidationError(
            f"导出文件项目 ID 为 {actual_project_id}，不是要求的 {project_id}"
        )
    if project.get("task_type") != "semantic_relevance":
        raise ValidationError("导出文件不是 semantic_relevance 项目")

    ready: list[ItemPlan] = []
    review: list[ItemPlan] = []
    for raw_item in export_data.get("items") or []:
        plan = build_item_plan(raw_item, email, include_submitted=include_submitted)
        if plan is None:
            continue
        if selected_item_ids and plan.item_id not in selected_item_ids:
            continue
        if all(pair.agreement >= min_agreement for pair in plan.pairs):
            ready.append(plan)
        else:
            review.append(plan)
    return ready, review


def plan_to_dict(
    export_path: Path,
    export_data: dict[str, Any],
    email: str,
    ready: list[ItemPlan],
    review: list[ItemPlan],
    min_agreement: float,
) -> dict[str, Any]:
    def serialize(item: ItemPlan) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "item_key": item.item_key,
            "title": item.title,
            "assignment_id": item.assignment_id,
            "payload": item.payload(),
            "pairs": [asdict(pair) for pair in item.pairs],
        }

    return {
        "schema_version": 1,
        "generated_at": now_text(),
        "source_export": str(export_path),
        "source_exported_at": export_data.get("exported_at"),
        "project": export_data.get("project"),
        "annotator_email": email,
        "label_source": "semantic_context.pairs[].llm_grade_final",
        "server_note": AI_NOTE,
        "minimum_agreement": min_agreement,
        "counts": {
            "ready_items": len(ready),
            "ready_pairs": sum(len(item.pairs) for item in ready),
            "review_items": len(review),
            "review_pairs": sum(len(item.pairs) for item in review),
        },
        "ready": [serialize(item) for item in ready],
        "needs_review": [serialize(item) for item in review],
    }


def own_submission(detail: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    submissions = (detail.get("submissions") or []) + (detail.get("latest_submissions") or [])
    for submission in submissions:
        if int(submission.get("annotator_user_id") or 0) == user_id:
            return submission
    return None


def own_live_assignment(
    detail: dict[str, Any], user_id: int, email: str
) -> dict[str, Any] | None:
    for assignment in detail.get("assignments") or []:
        if int(assignment.get("annotator_user_id") or 0) == user_id:
            return assignment
        if str(assignment.get("annotator_email") or "").casefold() == email.casefold():
            return assignment
    return None


def fetch_online_export(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    routes = load_routes(args.routes)
    password = os.environ.get(args.password_env) or getpass.getpass("网站密码: ")
    if not password:
        raise RuntimeError(f"在线导出模式需要先设置环境变量 {args.password_env}")

    client = ApiClient(args.base_url, routes)
    user = client.login(args.email, password)
    if str(user.get("email") or "").casefold() != args.email.casefold():
        raise RuntimeError(f"登录账号不是目标账号 {args.email}")

    project = client.get_route("project", project_id=args.project_id).get("project") or {}
    if int(project.get("id") or 0) != args.project_id:
        raise RuntimeError("在线项目 ID 校验失败")
    if project.get("task_type") != "semantic_relevance":
        raise RuntimeError("在线项目类型不是 semantic_relevance")

    export_data = client.get_route("project_export", project_id=args.project_id)
    if int((export_data.get("project") or {}).get("id") or 0) != args.project_id:
        raise RuntimeError("在线导出项目 ID 校验失败")
    return Path(f"online_project_{args.project_id}_export.json"), export_data


def validate_base_url(base_url: str, allow_insecure_http: bool) -> None:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("--base-url 必须是完整的 http(s) URL")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ValidationError(
            "默认要求 HTTPS 保护登录凭据；旧服务确实只支持 HTTP 时，"
            "请显式添加 --allow-insecure-http"
        )


def normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    answers = []
    for answer in payload.get("answers") or []:
        answers.append(
            {
                "pair_id": str(answer.get("pair_id") or ""),
                "relevance_grade": int(answer.get("relevance_grade")),
                "unable_to_judge": bool(answer.get("unable_to_judge")),
                "reason": str(answer.get("reason") or ""),
            }
        )
    return {"answers": answers, "note": str(payload.get("note") or "")}


def live_pair_ids(detail: dict[str, Any]) -> list[str]:
    candidates = [
        ((detail.get("item") or {}).get("initial_payload_json") or {}).get("pairs"),
        (detail.get("semantic_context") or {}).get("pairs"),
        (detail.get("payload") or {}).get("pairs"),
    ]
    for pairs in candidates:
        if isinstance(pairs, list) and pairs:
            return [str(pair.get("pair_id") or "") for pair in pairs]
    return []


def submit_plan(
    args: argparse.Namespace,
    ready: list[ItemPlan],
) -> list[ActionResult]:
    routes = load_routes(args.routes)
    password = os.environ.get(args.password_env) or getpass.getpass("网站密码: ")
    if not password:
        raise RuntimeError(f"提交模式需要先设置环境变量 {args.password_env}")

    client = ApiClient(args.base_url, routes)
    user = client.login(args.email, password)
    user_id = int(user.get("id") or 0)
    if not user_id:
        raise RuntimeError("登录用户缺少 id")
    if str(user.get("email") or "").casefold() != args.email.casefold():
        raise RuntimeError(f"登录账号不是目标账号 {args.email}")

    project = client.get_route("project", project_id=args.project_id).get("project") or {}
    if int(project.get("id") or 0) != args.project_id:
        raise RuntimeError("在线项目 ID 校验失败")
    if project.get("task_type") != "semantic_relevance":
        raise RuntimeError("在线项目类型不是 semantic_relevance")

    results: list[ActionResult] = []
    submitted_count = 0
    for index, item in enumerate(ready, 1):
        detail = client.get_route("item_detail", item_id=item.item_id)
        assignment = own_live_assignment(detail, user_id, args.email)
        if not assignment:
            result = ActionResult(item.item_id, item.title, "skipped", "不是本人任务")
        elif (
            assignment.get("status") == "submitted" or own_submission(detail, user_id)
        ) and not args.resubmit_from_log:
            result = ActionResult(item.item_id, item.title, "already_submitted", "在线已提交")
        else:
            expected_pair_ids = [pair.pair_id for pair in item.pairs]
            actual_pair_ids = live_pair_ids(detail)
            if actual_pair_ids and actual_pair_ids != expected_pair_ids:
                raise RuntimeError(
                    f"item #{item.item_id} 的在线 Query 与导出文件不一致，已停止"
                )
            payload = item.payload()
            current = (own_submission(detail, user_id) or {}).get("payload_json") or {}
            if args.resubmit_from_log and normalized_payload(current) == normalized_payload(
                payload
            ):
                result = ActionResult(
                    item.item_id,
                    item.title,
                    "already_current",
                    "在线答案与空整体备注已一致",
                )
            else:
                client.post_route("submit_item", payload, item_id=item.item_id)
                verify = client.get_route("item_detail", item_id=item.item_id)
                saved = (own_submission(verify, user_id) or {}).get("payload_json") or {}
                if normalized_payload(saved) != normalized_payload(payload):
                    raise RuntimeError(f"item #{item.item_id} 提交后回读不一致，已停止")
                submitted_count += 1
                action = "resubmitted" if args.resubmit_from_log else "submitted"
                result = ActionResult(item.item_id, item.title, action, "提交并回读一致")
                if args.interval_seconds > 0 and index < len(ready):
                    time.sleep(args.interval_seconds)

        results.append(result)
        print(
            f"[{index}/{len(ready)}] {result.action} #{item.item_id} {item.title} - {result.detail}",
            flush=True,
        )
    print(f"本次新提交：{submitted_count}")
    return results


def parse_args() -> argparse.Namespace:
    default_export = latest_default_export()
    parser = argparse.ArgumentParser(
        description="从语义相关性项目导出文件生成 AI 辅助预览，并可选提交"
    )
    parser.add_argument("--export", type=Path, default=default_export, help="项目导出 JSON")
    parser.add_argument(
        "--online-export",
        action="store_true",
        help="登录后从在线项目导出读取数据；需要 --base-url 和 --project-id",
    )
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--email", required=True, help="用于筛选本人任务及核对登录身份的账号")
    parser.add_argument("--base-url", help="标注服务根地址；在线导出或提交时必需")
    parser.add_argument("--routes", type=Path, default=Path("annotation_routes.json"), help="后端适配路由配置；默认读取当前目录 annotation_routes.json")
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="显式允许明文 HTTP；仅供可信网络中的旧服务使用",
    )
    parser.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV)
    parser.add_argument("--plan-output", type=Path, default=Path("semantic_relevance_plan.json"))
    parser.add_argument("--log", type=Path, default=Path("semantic_relevance_submit_log.json"))
    parser.add_argument("--item-id", type=int, action="append", default=[])
    parser.add_argument(
        "--resubmit-from-log",
        type=Path,
        help="只重提指定日志中 action=submitted/resubmitted 的本人条目",
    )
    parser.add_argument("--max-items", type=int, default=0, help="0 表示不限制")
    parser.add_argument(
        "--min-agreement",
        type=float,
        default=0.0,
        help="候选模型与最终等级的最低一致比例，0–1；默认处理全部",
    )
    parser.add_argument("--submit", action="store_true", help="真正提交；默认仅预览")
    parser.add_argument(
        "--acknowledge-ai-assisted",
        action="store_true",
        help="确认提交内容为 AI 辅助标注；提交模式必须提供",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=45.0,
        help="新提交之间的固定间隔；默认 45 秒，仅用于服务器稳定性",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    if not args.online_export and args.export is None:
        raise ValidationError("未找到默认导出文件，请用 --export 指定 JSON 路径")
    if args.online_export or args.submit:
        if not args.base_url:
            raise ValidationError("在线导出或提交时必须提供 --base-url")
        validate_base_url(args.base_url, args.allow_insecure_http)
    export_path = args.export.expanduser().resolve() if args.export is not None else None
    if not 0.0 <= args.min_agreement <= 1.0:
        raise ValidationError("--min-agreement 必须在 0–1 之间")
    if args.max_items < 0:
        raise ValidationError("--max-items 不能为负数")
    if args.interval_seconds < 0:
        raise ValidationError("--interval-seconds 不能为负数")
    if args.submit and not args.acknowledge_ai_assisted:
        raise ValidationError("提交模式必须同时提供 --acknowledge-ai-assisted")

    selected_item_ids = set(args.item_id)
    if args.resubmit_from_log:
        resubmit_log_path = args.resubmit_from_log.expanduser().resolve()
        resubmit_log = load_json(resubmit_log_path)
        log_project_id = int(resubmit_log.get("project_id") or 0)
        log_email = str(resubmit_log.get("annotator_email") or "")
        if log_project_id != args.project_id:
            raise ValidationError(
                f"重提日志项目 ID 为 {log_project_id}，不是要求的 {args.project_id}"
            )
        if log_email.casefold() != args.email.casefold():
            raise ValidationError("重提日志不属于目标账号")
        log_item_ids = {
            int(result["item_id"])
            for result in resubmit_log.get("results") or []
            if result.get("action") in {"submitted", "resubmitted"}
        }
        if not log_item_ids:
            raise ValidationError("重提日志中没有可处理的已提交条目")
        if selected_item_ids:
            selected_item_ids.intersection_update(log_item_ids)
            if not selected_item_ids:
                raise ValidationError("指定的 --item-id 不在重提日志中")
        else:
            selected_item_ids = log_item_ids

    if args.online_export:
        export_path, export_data = fetch_online_export(args)
    else:
        assert export_path is not None
        export_data = load_json(export_path)
    ready, review = build_plan(
        export_data=export_data,
        email=args.email,
        project_id=args.project_id,
        selected_item_ids=selected_item_ids,
        min_agreement=args.min_agreement,
        include_submitted=bool(args.resubmit_from_log),
    )
    if args.max_items:
        ready = ready[: args.max_items]

    assert export_path is not None
    plan = plan_to_dict(
        export_path,
        export_data,
        args.email,
        ready,
        review,
        args.min_agreement,
    )
    plan_path = args.plan_output.expanduser().resolve()
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = plan["counts"]
    print(f"项目：{(export_data.get('project') or {}).get('name')} ({args.project_id})")
    print(f"账号：{args.email}")
    print(
        f"可处理：{counts['ready_items']} 条 / {counts['ready_pairs']} 个 Query；"
        f"需复核：{counts['review_items']} 条"
    )
    print(f"预览计划：{plan_path}")

    results: list[ActionResult] = []
    if args.submit:
        results = submit_plan(args, ready)
    else:
        for item in ready[:20]:
            grades = "/".join(str(pair.grade) for pair in item.pairs)
            print(f"preview #{item.item_id} {item.title}: {grades}")
        if len(ready) > 20:
            print(f"……其余 {len(ready) - 20} 条见预览计划")

    log = {
        "generated_at": now_text(),
        "project_id": args.project_id,
        "annotator_email": args.email,
        "source_export": str(export_path),
        "submit_mode": bool(args.submit),
        "ai_assisted_acknowledged": bool(args.acknowledge_ai_assisted),
        "server_note": AI_NOTE,
        "results": [asdict(result) for result in results],
    }
    log_path = args.log.expanduser().resolve()
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"运行日志：{log_path}")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
