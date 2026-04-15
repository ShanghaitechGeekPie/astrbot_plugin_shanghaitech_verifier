import asyncio
import glob
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register

GRAD_VERIFY_MESSAGE = (
    "【学号验证】\n"
    "您好，您所在的选课群正在进行成员验证。\n"
    "请回复 /验证 <学号> 以完成验证。\n"
    "例如：/验证 2020000001\n"
    "如有疑问请联系群管理员。"
)


@register(
    "astrbot_plugin_shanghaitech_verifier",
    "ZAMBAR",
    "ShanghaiTech 进群学号校验",
    "1.2.0",
)
class ShanghaiTechVerifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / "data"
        self.students_path = self.data_dir / "students.json"
        self.verified_dir = self.data_dir / "verified"
        self.current_path = self.data_dir / "current.json"
        self.admin_group = str(self.config.get("admin_group", "") or "").strip()
        self.managed_group = str(self.config.get("managed_group", "") or "").strip()
        self.debug_log = bool(self.config.get("debug_log", True))
        self.graduated_path = self.data_dir / "graduated.json"
        self.grad_state_path = self.data_dir / "grad_verify_state.json"
        self._task_running = False
        self._verify_task: asyncio.Task | None = None
        self._bot = None

    def _debug(self, message: str) -> None:
        if self.debug_log:
            logger.debug(message)

    async def initialize(self):
        self._debug(
            "[initialize] verifier config loaded "
            f"admin_group={self.admin_group or '(empty)'} "
            f"managed_group={self.managed_group or '(empty)'} "
            f"debug_log={self.debug_log}"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.verified_dir.mkdir(parents=True, exist_ok=True)
        if not self.students_path.exists():
            self._debug("[initialize] students.json not found, creating with dummy data")
            self._write_students_index(self._dummy_students_index())
        else:
            self._debug(f"[initialize] students index ready at {self.students_path}")
        if not self.graduated_path.exists():
            self._debug("[initialize] graduated.json not found, creating empty")
            with self.graduated_path.open("w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        if not self.grad_state_path.exists():
            self._debug("[initialize] grad_verify_state.json not found, creating default")
            self._write_grad_state({"users": {}, "used_student_ids": {}, "task_enabled": False})

    # ── static helpers ──────────────────────────────────────────────

    @staticmethod
    def _dummy_students_index() -> dict[str, dict[str, Any]]:
        return {
            "2024000001": {
                "name": "张三",
                "email": "zhangsan@shanghaitech.edu.cn",
                "category": "本科生",
                "count": 0,
            },
            "2024000002": {
                "name": "李四",
                "email": "lisi@shanghaitech.edu.cn",
                "category": "研究生",
                "count": 0,
            },
            "2024000003": {
                "name": "王五",
                "email": "wangwu@shanghaitech.edu.cn",
                "category": "本科生",
                "count": 0,
            },
            "2024000004": {
                "name": "赵六",
                "email": "zhaoliu@shanghaitech.edu.cn",
                "category": "本科生",
                "count": 0,
            },
        }

    # ── students.json I/O ───────────────────────────────────────────

    def _read_students_index(self) -> dict[str, dict[str, Any]]:
        if not self.students_path.exists():
            self._debug("[_read_students_index] file missing, fallback to dummy index")
            return self._dummy_students_index()
        try:
            with self.students_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._debug(
                    f"[_read_students_index] loaded index entries={len(data.keys())}"
                )
                return data
        except Exception as err:
            logger.error(f"读取学生索引失败: {err}")
        return self._dummy_students_index()

    def _write_students_index(self, data: dict[str, dict[str, Any]]) -> None:
        with self.students_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._debug(f"[_write_students_index] wrote entries={len(data.keys())}")

    # ── graduated.json & grad_verify_state.json I/O ─────────────────

    def _read_graduated(self) -> dict[str, dict[str, Any]]:
        if not self.graduated_path.exists():
            return {}
        try:
            with self.graduated_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as err:
            logger.error(f"读取 graduated.json 失败: {err}")
        return {}

    def _read_grad_state(self) -> dict[str, Any]:
        default = {"users": {}, "used_student_ids": {}, "task_enabled": False}
        if not self.grad_state_path.exists():
            return default
        try:
            with self.grad_state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as err:
            logger.error(f"读取 grad_verify_state.json 失败: {err}")
        return default

    def _write_grad_state(self, data: dict[str, Any]) -> None:
        with self.grad_state_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── whitelist (verified QQ) ─────────────────────────────────────

    def _load_verified_qq_ids(self) -> dict[int, str]:
        """Load all verified QQ IDs from data/verified/20*qq.json files.

        Returns dict mapping user_id (int) -> card (str).
        """
        result: dict[int, str] = {}
        pattern = str(self.verified_dir / "20*qq.json")
        files = sorted(glob.glob(pattern))
        for filepath in files:
            fname = Path(filepath).name
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = json.load(f)
                members = content.get("data", [])
                count = 0
                for member in members:
                    uid = member.get("user_id")
                    card = member.get("card", "") or member.get("nickname", "")
                    if uid is not None:
                        result[int(uid)] = card
                        count += 1
                self._debug(f"[_load_verified_qq_ids] {fname}: loaded {count} QQ IDs")
            except Exception as err:
                logger.error(f"加载认证文件 {fname} 失败: {err}")
        self._debug(f"[_load_verified_qq_ids] total verified QQ IDs: {len(result)}")
        return result

    def _check_qq_in_whitelist(self, qq: int) -> tuple[bool, list[str]]:
        """Check if a QQ number is in any verified whitelist file.

        Returns (is_in_whitelist, list_of_source_filenames).
        """
        sources: list[str] = []
        pattern = str(self.verified_dir / "20*qq.json")
        files = sorted(glob.glob(pattern))
        for filepath in files:
            fname = Path(filepath).name
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = json.load(f)
                members = content.get("data", [])
                for member in members:
                    uid = member.get("user_id")
                    if uid is not None and int(uid) == qq:
                        sources.append(fname)
                        break
            except Exception as err:
                logger.error(f"检查白名单文件 {fname} 失败: {err}")
        return len(sources) > 0, sources

    # ── student ID extraction & validation ──────────────────────────

    @staticmethod
    def _extract_student_id(comment: str) -> str | None:
        if not comment:
            return None
        answer_match = re.search(r"答案[:：]\s*(\d{10})", comment)
        if answer_match:
            return answer_match.group(1)
        direct_match = re.search(r"\b(\d{10})\b", comment)
        if direct_match:
            return direct_match.group(1)
        return None

    def _validate_student(
        self, student_id: str | None
    ) -> tuple[bool, str, dict[str, Any] | None]:
        self._debug(f"[_validate_student] student_id={student_id}")
        if not student_id:
            return False, "未提供 10 位学号", None

        students = self._read_students_index()
        record = students.get(student_id)
        if not isinstance(record, dict):
            return False, f"学号 {student_id} 不在索引中", None

        category = str(record.get("category", ""))
        if category != "本科生":
            return False, f"学号 {student_id} 类别为 {category}，非本科生", record

        count = int(record.get("count", 0))
        if count != 0:
            return False, f"学号 {student_id} 已使用过（count={count}）", record

        return True, "校验通过", record

    def _mark_student_used(self, student_id: str) -> None:
        students = self._read_students_index()
        record = students.get(student_id)
        if not isinstance(record, dict):
            self._debug(f"[_mark_student_used] record missing for student_id={student_id}")
            return
        count = int(record.get("count", 0))
        record["count"] = count + 1
        students[student_id] = record
        self._write_students_index(students)
        self._debug(
            f"[_mark_student_used] student_id={student_id} count {count} -> {count + 1}"
        )

    # ── admin notification ──────────────────────────────────────────

    async def _notify_admin_group(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        comment: str,
        student_id: str | None,
        reason: str,
    ) -> None:
        if not self.admin_group:
            logger.warning(
                "检测到异常进群申请，但未配置 config.admin_group，无法发送管理员群告警"
            )
            return

        message = (
            "【进群异常】\n"
            f"群号: {group_id}\n"
            f"QQ: {user_id}\n"
            f"答案学号: {student_id or '无'}\n"
            f"原因: {reason}\n"
            f"原始申请: {comment or '无'}\n"
            "处理: 已告警，不自动拒绝入群"
        )
        try:
            target_group = int(self.admin_group)
            await event.bot.send_group_msg(
                group_id=target_group,
                message=message,
            )
            self._debug(
                f"[_notify_admin_group] sent alert to admin_group={target_group} for user={user_id}"
            )
        except ValueError:
            logger.error(f"config.admin_group 非法，无法转为整数: {self.admin_group}")
        except Exception as err:
            logger.error(f"发送管理员群告警失败: {err}")

    # ── group member fetching ───────────────────────────────────────

    async def _fetch_group_members(self, bot, group_id: int) -> list[dict]:
        try:
            result = await bot.get_group_member_list(group_id=group_id)
            if isinstance(result, list):
                return result
            logger.error(f"get_group_member_list 返回非列表类型: {type(result)}")
            return []
        except Exception as err:
            logger.error(f"获取群 {group_id} 成员列表失败: {err}")
            return []

    # ── group add request handler (dual verification) ───────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_add_request(self, event: AstrMessageEvent):
        self._debug("[on_group_add_request] event received")
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            self._debug(
                f"[on_group_add_request] skip: raw_message is {type(raw).__name__}, not dict"
            )
            return

        self._debug(
            "[on_group_add_request] raw brief "
            f"post_type={raw.get('post_type')} request_type={raw.get('request_type')} "
            f"sub_type={raw.get('sub_type')} group_id={raw.get('group_id')} user_id={raw.get('user_id')}"
        )

        if (
            raw.get("post_type") != "request"
            or raw.get("request_type") != "group"
            or raw.get("sub_type") != "add"
        ):
            self._debug("[on_group_add_request] skip: not a group add request event")
            return

        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        flag = raw.get("flag", "")
        comment = str(raw.get("comment", "") or "")
        self._debug(
            f"[on_group_add_request] handling request group_id={group_id} user_id={user_id} flag={flag} comment={comment!r}"
        )

        # Step 1: Check QQ whitelist
        qq_int = int(user_id) if user_id.isdigit() else 0
        in_whitelist, wl_sources = self._check_qq_in_whitelist(qq_int)
        self._debug(
            f"[on_group_add_request] whitelist check: in_whitelist={in_whitelist} sources={wl_sources}"
        )

        # Step 2: Extract and validate student ID
        student_id = self._extract_student_id(comment)
        self._debug(f"[on_group_add_request] extracted student_id={student_id}")
        approved, reason, _ = self._validate_student(student_id)
        self._debug(
            f"[on_group_add_request] validation result approved={approved} reason={reason}"
        )

        # Step 3: Dual verification — both whitelist and student validation must pass
        if in_whitelist and approved and student_id:
            try:
                await event.bot.set_group_add_request(
                    flag=flag,
                    sub_type="add",
                    approve=True,
                    reason="",
                )
                self._mark_student_used(student_id)
                logger.info(
                    f"已自动放行进群申请: 群={group_id}, QQ={user_id}, 学号={student_id}"
                )
            except Exception as err:
                logger.error(f"自动放行进群申请失败: {err}")
            return

        # Build combined reason for alert
        reasons = []
        if not in_whitelist:
            reasons.append(f"QQ {user_id} 不在认证白名单中")
        if not approved:
            reasons.append(reason)
        combined_reason = "；".join(reasons)

        self._debug("[on_group_add_request] request not approved, notifying admin group")
        await self._notify_admin_group(
            event=event,
            group_id=group_id,
            user_id=user_id,
            comment=comment,
            student_id=student_id,
            reason=combined_reason,
        )

    # ── graduation verification loop ────────────────────────────────

    async def _verification_loop(self):
        self._debug("[_verification_loop] started")
        while self._task_running:
            try:
                await self._send_next_verification()
            except Exception as err:
                logger.error(f"[_verification_loop] error: {err}")
            await asyncio.sleep(3600)
        self._debug("[_verification_loop] stopped")

    async def _send_next_verification(self):
        if not self.current_path.exists():
            self._debug("[_send_next_verification] current.json not found, skip")
            return
        try:
            with self.current_path.open("r", encoding="utf-8") as f:
                current = json.load(f)
        except Exception as err:
            logger.error(f"[_send_next_verification] read current.json failed: {err}")
            return

        current_members = current.get("data", [])
        verified = self._load_verified_qq_ids()
        state = self._read_grad_state()
        users = state.setdefault("users", {})

        for m in current_members:
            uid = m.get("user_id")
            if uid is None:
                continue
            uid_int = int(uid)
            qq_str = str(uid_int)

            if uid_int in verified:
                continue
            user_state = users.get(qq_str, {})
            status = user_state.get("status")
            if status in ("verified", "sent"):
                continue

            # This user needs a verification message
            try:
                await self._bot.send_private_msg(user_id=uid_int, message=GRAD_VERIFY_MESSAGE)
                users[qq_str] = {
                    "status": "sent",
                    "student_id": None,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "verified_at": None,
                    "reason": "",
                }
                state["users"] = users
                self._write_grad_state(state)
                self._debug(f"[_send_next_verification] sent to {qq_str}")
                return  # one per cycle
            except Exception as err:
                logger.error(f"[_send_next_verification] failed to send to {qq_str}: {err}")
                users[qq_str] = {
                    "status": "failed",
                    "student_id": None,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "verified_at": None,
                    "reason": f"无法发送私聊消息: {err}",
                }
                state["users"] = users
                self._write_grad_state(state)
                # continue to next user

        self._debug("[_send_next_verification] no more users to verify this cycle")

    # ── private message reply handler ─────────────────────────────

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_private_verify_reply(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "message" or raw.get("message_type") != "private":
            return

        sender_qq = str(raw.get("user_id", ""))
        if not sender_qq:
            return

        state = self._read_grad_state()
        users = state.get("users", {})
        user_state = users.get(sender_qq, {})
        if user_state.get("status") != "sent":
            return

        text = str(raw.get("raw_message", "") or event.message_str or "").strip()
        verify_match = re.search(r"/验证\s*(\d{10})\b", text)
        if not verify_match:
            try:
                await event.bot.send_private_msg(
                    user_id=int(sender_qq),
                    message="请使用正确格式：/验证 <10位学号>\n例如：/验证 2020000001",
                )
            except Exception:
                pass
            return
        student_id = verify_match.group(1)

        graduated = self._read_graduated()
        used = state.get("used_student_ids", {})

        if student_id not in graduated:
            user_state["status"] = "failed"
            user_state["reason"] = f"学号 {student_id} 不在毕业生名单中"
            users[sender_qq] = user_state
            state["users"] = users
            self._write_grad_state(state)
            try:
                await event.bot.send_private_msg(
                    user_id=int(sender_qq),
                    message=f"验证失败：学号 {student_id} 不在毕业生名单中，请联系管理员。",
                )
            except Exception:
                pass
            return

        if student_id in used and used[student_id] != sender_qq:
            user_state["status"] = "failed"
            user_state["reason"] = f"学号 {student_id} 已绑定其他 QQ"
            users[sender_qq] = user_state
            state["users"] = users
            self._write_grad_state(state)
            try:
                await event.bot.send_private_msg(
                    user_id=int(sender_qq),
                    message=f"验证失败：学号 {student_id} 已绑定其他 QQ，请联系管理员。",
                )
            except Exception:
                pass
            return

        # Verification passed
        now = datetime.now(timezone.utc).isoformat()
        user_state["status"] = "verified"
        user_state["student_id"] = student_id
        user_state["verified_at"] = now
        user_state["reason"] = ""
        users[sender_qq] = user_state
        used[student_id] = sender_qq
        state["users"] = users
        state["used_student_ids"] = used
        self._write_grad_state(state)
        self._debug(f"[on_private_verify_reply] {sender_qq} verified with {student_id}")
        try:
            await event.bot.send_private_msg(
                user_id=int(sender_qq),
                message="验证成功！感谢您的配合。",
            )
        except Exception:
            pass

    # ── admin group commands ────────────────────────────────────────

    def _is_admin_group(self, event: AstrMessageEvent) -> bool:
        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id or not self.admin_group:
            return False
        return str(group_id) == self.admin_group

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示群助手帮助信息"""
        if not self._is_admin_group(event):
            return
        help_text = (
            "【ShanghaiTech 群助手】\n"
            "/帮助 - 显示本帮助\n"
            "/扫描成员 - 扫描选课群成员并与认证名单比对\n"
            "/查询 <QQ号> - 查询指定 QQ 是否已认证及入群状态\n"
            "/开始任务 - 启动毕业生验证（需先扫描成员）\n"
            "/暂停任务 - 暂停毕业生验证任务\n"
            "/任务状态 - 查看验证任务进度与统计"
        )
        yield event.plain_result(help_text)

    @filter.command("扫描成员")
    async def cmd_scan_members(self, event: AstrMessageEvent):
        """扫描选课群成员并与认证名单比对"""
        if not self._is_admin_group(event):
            return

        if not self.managed_group:
            yield event.plain_result("未配置 managed_group（选课群号），无法扫描。")
            return

        try:
            managed_gid = int(self.managed_group)
        except ValueError:
            yield event.plain_result(f"managed_group 配置无效: {self.managed_group}")
            return

        # Fetch current group members
        members = await self._fetch_group_members(event.bot, managed_gid)
        if not members:
            yield event.plain_result("获取群成员列表失败或群内无成员。")
            return

        # Save to current.json
        try:
            with self.current_path.open("w", encoding="utf-8") as f:
                json.dump({"status": "ok", "data": members}, f, ensure_ascii=False, indent=2)
            self._debug(f"[cmd_scan_members] saved {len(members)} members to current.json")
        except Exception as err:
            logger.error(f"保存 current.json 失败: {err}")

        # Load whitelist
        verified = self._load_verified_qq_ids()

        # Compare
        verified_members = []
        unverified_members = []
        for m in members:
            uid = m.get("user_id")
            if uid is None:
                continue
            uid_int = int(uid)
            display = m.get("card", "") or m.get("nickname", "") or str(uid_int)
            if uid_int in verified:
                verified_members.append((uid_int, display))
            else:
                unverified_members.append((uid_int, display))

        # Build forwarded message nodes
        bot_id = str(getattr(event.bot, "qq", 0) or 10000)
        bot_name = "群助手"

        # Node 1: Summary
        summary = (
            f"【扫描结果 - 群 {managed_gid}】\n"
            f"白名单人数: {len(verified)}\n"
            f"群成员数: {len(members)}\n"
            f"已认证: {len(verified_members)}\n"
            f"未认证: {len(unverified_members)}"
        )
        node_summary = Node(uin=bot_id, name=bot_name, content=[Plain(summary)])

        # Node 2: Verified list
        if len(verified_members) <= 50:
            verified_lines = [f"{uid} - {name}" for uid, name in verified_members]
            verified_text = f"【已认证成员 ({len(verified_members)})】\n" + "\n".join(verified_lines)
        else:
            verified_text = f"【已认证成员】共 {len(verified_members)} 人（数量过多，省略列表）"
        node_verified = Node(uin=bot_id, name=bot_name, content=[Plain(verified_text)])

        # Node 3: Unverified list
        unverified_lines = [f"{uid} - {name}" for uid, name in unverified_members]
        if unverified_lines:
            unverified_text = f"【未认证成员 ({len(unverified_members)})】\n" + "\n".join(unverified_lines)
        else:
            unverified_text = "【未认证成员】无"
        node_unverified = Node(uin=bot_id, name=bot_name, content=[Plain(unverified_text)])

        yield event.chain_result([node_summary, node_verified, node_unverified])

    @filter.command("查询")
    async def cmd_query(self, event: AstrMessageEvent):
        """查询指定 QQ 是否已认证及入群状态"""
        if not self._is_admin_group(event):
            return

        # Extract QQ number from command arguments
        text = event.message_str.strip()
        self._debug(f"[cmd_query] raw message_str={text!r}")
        # Find the first sequence of digits (QQ number) in the text
        qq_match = re.search(r"(\d{5,12})", text)
        if not qq_match:
            yield event.plain_result("用法: /查询 <QQ号>")
            return

        qq_str = qq_match.group(1)
        try:
            qq_int = int(qq_str)
        except ValueError:
            yield event.plain_result(f"无效的 QQ 号: {qq_str}")
            return

        lines: list[str] = [f"【查询结果 - QQ {qq_int}】"]

        # Check whitelist
        in_wl, sources = self._check_qq_in_whitelist(qq_int)
        if in_wl:
            lines.append(f"认证白名单: 已认证（来源: {', '.join(sources)}）")
        else:
            lines.append("认证白名单: 未认证")

        # Check current.json (latest scan)
        if self.current_path.exists():
            try:
                with self.current_path.open("r", encoding="utf-8") as f:
                    current = json.load(f)
                current_members = current.get("data", [])
                found_in_group = False
                for m in current_members:
                    if m.get("user_id") is not None and int(m["user_id"]) == qq_int:
                        display = m.get("card", "") or m.get("nickname", "") or str(qq_int)
                        lines.append(f"选课群状态: 在群中（{display}）")
                        found_in_group = True
                        break
                if not found_in_group:
                    lines.append("选课群状态: 不在群中（基于最近一次扫描）")
            except Exception as err:
                lines.append(f"选课群状态: 读取 current.json 失败（{err}）")
        else:
            lines.append("选课群状态: 尚未扫描（请先执行 /扫描成员）")

        # Check students.json for associated records
        students = self._read_students_index()
        matched_records = []
        for sid, record in students.items():
            # We can't directly match QQ to student ID from students.json
            # but we check if any record has this QQ associated (future extension)
            pass
        # For now, just report if user searches with a student ID-like number
        if len(qq_str) == 10 and qq_str in students:
            record = students[qq_str]
            lines.append(
                f"学号记录: {qq_str} - {record.get('name', '?')} "
                f"({record.get('category', '?')}, count={record.get('count', 0)})"
            )

        yield event.plain_result("\n".join(lines))

    @filter.command("开始任务")
    async def cmd_start_task(self, event: AstrMessageEvent):
        """启动毕业生验证任务"""
        if not self._is_admin_group(event):
            return

        if not self.current_path.exists():
            yield event.plain_result("请先执行 /扫描成员 获取群成员列表。")
            return

        self._bot = event.bot
        self._task_running = True
        state = self._read_grad_state()
        state["task_enabled"] = True
        self._write_grad_state(state)
        self._verify_task = asyncio.create_task(self._verification_loop())
        yield event.plain_result("毕业生验证任务已启动，每小时发送一条验证消息。")

    @filter.command("暂停任务")
    async def cmd_stop_task(self, event: AstrMessageEvent):
        """暂停毕业生验证任务"""
        if not self._is_admin_group(event):
            return

        self._task_running = False
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = None
        state = self._read_grad_state()
        state["task_enabled"] = False
        self._write_grad_state(state)
        yield event.plain_result("毕业生验证任务已暂停。")

    @filter.command("任务状态")
    async def cmd_task_status(self, event: AstrMessageEvent):
        """查看毕业生验证任务状态"""
        if not self._is_admin_group(event):
            return

        state = self._read_grad_state()
        users = state.get("users", {})
        enabled = state.get("task_enabled", False)

        counts = {"pending": 0, "sent": 0, "verified": 0, "failed": 0}
        verified_list: list[str] = []
        unverified_list: list[str] = []
        failed_list: list[str] = []

        for qq, info in users.items():
            status = info.get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
            if status == "verified":
                sid = info.get("student_id", "?")
                verified_list.append(f"{qq} - 学号 {sid}")
            elif status == "sent":
                unverified_list.append(f"{qq} - 已发送待回复")
            elif status == "failed":
                reason = info.get("reason", "未知")
                failed_list.append(f"{qq} - {reason}")

        bot_id = str(getattr(event.bot, "qq", 0) or 10000)
        bot_name = "群助手"

        summary = (
            f"【毕业生验证任务状态】\n"
            f"任务状态: {'运行中' if enabled and self._task_running else '已暂停'}\n"
            f"待处理: {counts['pending']}\n"
            f"已发送: {counts['sent']}\n"
            f"已验证: {counts['verified']}\n"
            f"失败: {counts['failed']}"
        )
        node_summary = Node(uin=bot_id, name=bot_name, content=[Plain(summary)])

        verified_text = f"【已验证 ({counts['verified']})】\n" + ("\n".join(verified_list) or "无")
        node_verified = Node(uin=bot_id, name=bot_name, content=[Plain(verified_text)])

        other_lines = []
        if unverified_list:
            other_lines.append(f"【待回复 ({counts['sent']})】")
            other_lines.extend(unverified_list)
        if failed_list:
            other_lines.append(f"\n【失败 ({counts['failed']})】")
            other_lines.extend(failed_list)
        other_text = "\n".join(other_lines) if other_lines else "无未验证/失败记录"
        node_other = Node(uin=bot_id, name=bot_name, content=[Plain(other_text)])

        yield event.chain_result([node_summary, node_verified, node_other])

    async def terminate(self):
        self._task_running = False
        if self._verify_task and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = None
