import glob
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import Context, Star, register


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
            "/查询 <QQ号> - 查询指定 QQ 是否已认证及入群状态"
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
        bot_id = getattr(event.bot, "qq", 0) or 10000
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
        parts = text.split()
        if len(parts) < 1 or not parts[0].isdigit():
            yield event.plain_result("用法: /查询 <QQ号>")
            return

        qq_str = parts[0]
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

    async def terminate(self):
        return
