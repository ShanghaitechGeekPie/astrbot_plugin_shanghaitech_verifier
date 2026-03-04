import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_shanghaitech_verifier",
    "ZAMBAR",
    "ShanghaiTech 进群学号校验",
    "1.1.0",
)
class ShanghaiTechVerifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / "data"
        self.students_path = self.data_dir / "students.json"
        self.admin_group = str(self.config.get("admin_group", "") or "").strip()
        self.debug_log = bool(self.config.get("debug_log", True))

    def _debug(self, message: str) -> None:
        if self.debug_log:
            logger.debug(message)

    async def initialize(self):
        self._debug(
            "[initialize] verifier config loaded "
            f"admin_group={self.admin_group or '(empty)'} debug_log={self.debug_log}"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.students_path.exists():
            self._debug("[initialize] students.json not found, creating with dummy data")
            self._write_students_index(self._dummy_students_index())
        else:
            self._debug(f"[initialize] students index ready at {self.students_path}")

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

        student_id = self._extract_student_id(comment)
        self._debug(f"[on_group_add_request] extracted student_id={student_id}")
        approved, reason, _ = self._validate_student(student_id)
        self._debug(
            f"[on_group_add_request] validation result approved={approved} reason={reason}"
        )

        if approved and student_id:
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

        self._debug("[on_group_add_request] request not approved, notifying admin group")
        await self._notify_admin_group(
            event=event,
            group_id=group_id,
            user_id=user_id,
            comment=comment,
            student_id=student_id,
            reason=reason,
        )

    async def terminate(self):
        return
