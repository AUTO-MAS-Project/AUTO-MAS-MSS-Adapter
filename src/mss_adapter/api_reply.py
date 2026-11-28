#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team

#   This file is part of AUTO-MAS.

#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.

#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty
#   of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
#   the GNU Affero General Public License for more details.

#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

#   Contact: DLmaster_361@163.com

"""MSS 外壳导入端点的结果载体。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MaaFWApiReply:
    """保留外壳导入接口的状态码、提示和逐实例结果。"""

    code: int = 200
    message: str | None = None
    data: Any = None

    @classmethod
    def error(cls, code: int, message: str, data: Any = None) -> MaaFWApiReply:
        return cls(code=code, message=message, data=data)

    def out_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self.code != 200:
            fields["code"] = self.code
            fields["status"] = "error"
        if self.message is not None:
            fields["message"] = self.message
        if self.data is not None:
            fields["data"] = self.data
        return fields


__all__ = ["MaaFWApiReply"]
