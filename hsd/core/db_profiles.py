"""Persistent provider/model profiles for HSD workflow activities."""

from hsd.core.db_connection import _immediate, _utcnow
from hsd.core.db_schema import AGENT_PURPOSES
from hsd.core.models import AgentProfile


class AgentProfileQueriesMixin:
    def list_agent_profiles(self) -> list[AgentProfile]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT purpose, provider, model, updated_at FROM agent_profiles"
        ).fetchall()
        by_purpose = {
            row["purpose"]: AgentProfile(**dict(row))
            for row in rows
        }
        return [
            by_purpose.get(
                purpose,
                AgentProfile(purpose=purpose, provider="", model="", updated_at=""),
            )
            for purpose in AGENT_PURPOSES
        ]

    def set_agent_profile(
        self,
        purpose: str,
        provider: str,
        model: str,
    ) -> AgentProfile:
        if purpose not in AGENT_PURPOSES:
            raise ValueError(
                f"invalid agent purpose: {purpose!r} (valid: {list(AGENT_PURPOSES)})"
            )
        provider = provider.strip()
        model = model.strip()
        now = _utcnow()
        conn = self._conn()
        with _immediate(conn):
            conn.execute(
                """INSERT INTO agent_profiles (purpose, provider, model, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(purpose) DO UPDATE SET
                       provider = excluded.provider,
                       model = excluded.model,
                       updated_at = excluded.updated_at""",
                (purpose, provider, model, now),
            )
        return AgentProfile(
            purpose=purpose,
            provider=provider,
            model=model,
            updated_at=now,
        )

    def set_agent_profiles(
        self,
        profiles: dict[str, tuple[str, str]],
    ) -> list[AgentProfile]:
        invalid = set(profiles) - set(AGENT_PURPOSES)
        if invalid:
            raise ValueError(f"invalid agent purposes: {sorted(invalid)}")
        now = _utcnow()
        conn = self._conn()
        with _immediate(conn):
            for purpose, (provider, model) in profiles.items():
                conn.execute(
                    """INSERT INTO agent_profiles (purpose, provider, model, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(purpose) DO UPDATE SET
                           provider = excluded.provider,
                           model = excluded.model,
                           updated_at = excluded.updated_at""",
                    (purpose, provider.strip(), model.strip(), now),
                )
        return self.list_agent_profiles()
