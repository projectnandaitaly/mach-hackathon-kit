"""Policy epoch applied by signed city orders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cap.core.errors import CapError
from cap.core.jcs import digest


class PolicyStore:
    def __init__(self) -> None:
        self.epoch = 0
        self.digest = digest({"epoch": 0})

    def apply(self, order: Mapping[str, Any], *, msg_id: str | None) -> None:
        epoch = order.get("epoch")
        if type(epoch) is not int or epoch != self.epoch + 1:
            raise CapError("STALE_EPOCH", "stale policy epoch", msg_id)
        order_id = order.get("order_id")
        self.epoch = epoch
        self.digest = digest({"epoch": epoch, "order_id": order_id})
