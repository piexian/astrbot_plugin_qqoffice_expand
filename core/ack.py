"""共享自动和手动互动应答资格及发送阶段。"""

from .errors import QQOfficeDuplicateAck


class AckPhase:
    """记录进入 SDK、明确拒绝与结果未知的阶段。"""

    def __init__(self):
        self.reached_sdk = False
        self.unknown = False
        self.definitive_rejected = False


class AckReservation(AckPhase):
    """排队前预留资格并持有机器人状态，结束时只结算一次。"""

    def __init__(self, state, interaction_id: str):
        super().__init__()
        self.state = state
        self.interaction_id = str(interaction_id)
        self.claimed = False
        self.finished = False
        if state.acks.try_reserve(self.interaction_id) != "new":
            raise QQOfficeDuplicateAck("该互动已在应答或已应答")
        state.retain()

    def claim(self, state, interaction_id: str) -> None:
        if (
            self.finished
            or self.claimed
            or state is not self.state
            or str(interaction_id) != self.interaction_id
        ):
            raise QQOfficeDuplicateAck("互动应答资格已使用或来源不匹配")
        self.claimed = True

    def finish(self, *, success: bool = False) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            if success and not self.unknown:
                self.state.acks.succeed(self.interaction_id)
            elif self.definitive_rejected or not self.reached_sdk:
                self.state.acks.release(self.interaction_id)
            else:
                self.state.acks.settle_unknown(self.interaction_id)
        finally:
            self.state.release()
