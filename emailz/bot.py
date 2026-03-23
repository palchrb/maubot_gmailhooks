import re
import json
import time
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from aiohttp import ClientResponse

from maubot import Plugin
from maubot.handlers import command
from mautrix.types import RoomID, UserID, EventType, PowerLevelStateEventContent
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .migrations import upgrade_table  # keep migrations in a sibling file


# ==========================
# Config
# ==========================
class PluginConfig(BaseProxyConfig):
    def do_update(self, h: ConfigUpdateHelper) -> None:
        # Apps Script admin API (Gmail bridge)
        h.copy("admin_base_url")            # e.g. https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec
        h.copy("admin_token")               # secret for ?admin_token=...

        # Gmail base address (we insert +<alias> before the @)
        # Example: "email@example.com" -> "email+<alias>@example.com"
        h.copy("gmail_base_address")

        # Alias policy
        h.copy("alias_append_random")       # bool: add -<random> suffix
        h.copy("alias_random_len")          # length of random suffix (digits+lowercase)

        # Command access control
        h.copy("restrict_commands_to_local")
        h.copy("local_homeserver_domain")
        h.copy("pl_required")
        h.copy("adminlist")

        # Output
        h.copy("show_webhook_in_check")     # include webhook host/path in !gmail check


# ==========================
# Gmail Subscriptions Control-Plane Plugin
# ==========================
class EmailSubscribePlugin(Plugin):
    config: PluginConfig

    # --------------- Boilerplate ---------------
    @classmethod
    def get_config_class(cls):
        return PluginConfig

    @classmethod
    def get_db_upgrade_table(cls):
        return upgrade_table

    async def start(self) -> None:
        self.config.load_and_update()
        # sensible defaults
        if self.config.get("alias_append_random", None) is None:
            self.config["alias_append_random"] = True
        if not self.config.get("alias_random_len", None):
            self.config["alias_random_len"] = 8
        if not self.config.get("gmail_base_address", None):
            # fallback for dev; recommended to set explicitly in config
            self.config["gmail_base_address"] = "email@example.com"
        self.log.info("GmailSubscribePlugin ready. Admin base URL: %s", self.config.get("admin_base_url", None))

    # --------------- Helpers ---------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _user_domain(self, user: UserID) -> str:
        try:
            server = str(user).split(":", 1)[1]
        except Exception:
            return ""
        return server.split(":", 1)[0].lower()

    def _is_local(self, user: UserID) -> bool:
        want = (self.config.get("local_homeserver_domain", "") or "").lower()
        have = self._user_domain(user)
        return (not want) or (have == want)

    async def _owner_pl_if_v12(self, room_id: RoomID, user: UserID) -> Optional[int]:
        """
        Return a very high PL if user is a v12+ creator (sender of m.room.create)
        or appears in additional_creators. Mirrors roomwebhook behavior.
        """
        ev = None
        try:
            ev = await self.client.get_state_event(room_id, EventType.ROOM_CREATE)
        except Exception:
            pass

        content: Dict[str, Any] = {}
        sender = ""

        if isinstance(ev, dict):
            sender = str(ev.get("sender", "") or "")
            c = ev.get("content")
            if isinstance(c, dict):
                content = c
        elif ev is not None:
            try:
                if hasattr(ev, "serialize"):
                    content = ev.serialize()  # type: ignore[attr-defined]
                else:
                    content = getattr(ev, "__dict__", {}) or {}
            except Exception:
                content = getattr(ev, "__dict__", {}) or {}

        rv = (content or {}).get("room_version")
        try:
            rv_int = int(str(rv))
        except Exception:
            rv_int = None

        # v12+: if sender missing, fetch raw /state to find it
        if rv_int is not None and rv_int >= 12 and not sender:
            try:
                from urllib.parse import quote
                path = f"/_matrix/client/v3/rooms/{quote(str(room_id))}/state"
                raw_state = await self.client.api.request("GET", path)
                if isinstance(raw_state, list):
                    for e in raw_state:
                        if isinstance(e, dict) and e.get("type") == "m.room.create" and (e.get("state_key", "") == ""):
                            sender = str(e.get("sender", "") or "")
                            c = e.get("content")
                            if not content and isinstance(c, dict):
                                content = c
                            break
            except Exception:
                pass

        # v12 rule: creator or in additional_creators => effectively unlimited
        if rv_int is not None and rv_int >= 12:
            addl = (content or {}).get("additional_creators") or []
            if sender and (sender == str(user) or (isinstance(addl, list) and str(user) in addl)):
                return 1_000_000  # effectively “owner”

        # Pre-v12: 'creator' field
        creator = (content or {}).get("creator")
        if creator and creator == str(user):
            return 100

        return None

    async def _get_user_pl(self, room_id: RoomID, user: UserID) -> int:
        # v12 owner fast-path
        owner_pl = await self._owner_pl_if_v12(room_id, user)
        if owner_pl is not None:
            return owner_pl

        # Standard power_levels lookup
        try:
            ev = await self.client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
        except Exception:
            return 0
        try:
            if isinstance(ev, PowerLevelStateEventContent):
                pls = ev
            elif isinstance(ev, dict):
                pls = PowerLevelStateEventContent.deserialize(ev)
            elif hasattr(ev, "content"):
                c = ev.content
                if isinstance(c, PowerLevelStateEventContent):
                    pls = c
                elif isinstance(c, dict):
                    pls = PowerLevelStateEventContent.deserialize(c)
                else:
                    pls = PowerLevelStateEventContent.deserialize(getattr(c, "__dict__", {}))
            else:
                pls = PowerLevelStateEventContent.deserialize(getattr(ev, "__dict__", {}))
        except Exception:
            return 0
        level = pls.users.get(user)
        if level is None:
            level = pls.users.get(str(user))
        if level is None:
            level = pls.users_default or 0
        try:
            return int(level or 0)
        except Exception:
            return 0

    async def _require_perms(self, room_id: RoomID, sender: UserID) -> bool:
        # adminlist bypass
        if sender in set(self.config.get("adminlist", []) or []):
            return True
        # restrict to local homeserver?
        if self.config.get("restrict_commands_to_local", False) and not self._is_local(sender):
            return False
        # power level
        required = int(self.config.get("pl_required", 0) or 0)
        if required <= 0:
            return True
        level = await self._get_user_pl(room_id, sender)
        return level >= required

    # ---- alias / webhook normalization ----
    _alias_re = re.compile(r"^[a-z0-9._-]{1,64}$")  # matches backend normAlias()

    def _norm_alias(self, alias: str) -> Optional[str]:
        a = (alias or "").strip().lower()
        return a if self._alias_re.match(a) else None

    def _is_https(self, url: str) -> bool:
        try:
            p = urlparse(url)
            return p.scheme == "https" and bool(p.netloc)
        except Exception:
            return False

    def _strip_query_token(self, url: str) -> Tuple[str, Optional[str]]:
        """Remove access_token/token from query string and return (clean_url, token or None)."""
        try:
            p = urlparse(url)
            q = parse_qs(p.query)
            token = None
            for key in ("access_token", "token"):
                if key in q and q[key]:
                    token = q[key][0]
                    del q[key]
            new_q = urlencode({k: v[0] if isinstance(v, list) and v else v for k, v in q.items()})
            clean = urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))
            return clean, token
        except Exception:
            return url, None

    def _maybe_token_from_path(self, url: str) -> Optional[str]:
        try:
            p = urlparse(url)
            parts = [x for x in p.path.split("/") if x]
            # accept .../hook/.../<token> as a last resort
            if len(parts) >= 2 and parts[-2] == "hook":
                cand = parts[-1]
                if re.fullmatch(r"[A-Za-z0-9._~+\-\/=]{8,}", cand):
                    return cand
        except Exception:
            pass
        return None

    def _hostish(self, url: str, maxlen: int = 64) -> str:
        try:
            p = urlparse(url)
            base = p.netloc + p.path
        except Exception:
            return url
        return (base[:maxlen] + "…") if len(base) > maxlen else base

    def _gmail_address(self, alias: str) -> str:
        base = str(self.config.get("gmail_base_address", "email@example.com") or "email@example.com").strip()
        if "@" not in base:
            return base  # fallback (misconfigured)
        local, domain = base.split("@", 1)
        return f"{local}+{alias}@{domain}"

    # --------------- Admin API calls ---------------
    async def _admin_call(self, action: str, body: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
        base = self.config.get("admin_base_url", None)
        token = self.config.get("admin_token", None)
        if not base or not token:
            return False, {"error": "missing_admin_config"}
        params = {"admin_token": token, "action": action}
        url = base
        try:
            resp: ClientResponse
            if body is None:
                # GETs follow redirects by default in aiohttp
                resp = await self.http.get(url, params=params, allow_redirects=True)
            else:
                # IMPORTANT: follow redirects for POST (Apps Script often redirects to googleusercontent)
                resp = await self.http.post(url, params=params, json=body, allow_redirects=True)
            txt = await resp.text()
            try:
                data = json.loads(txt)
            except Exception:
                data = {"raw": txt}
            ok = (200 <= resp.status < 300) and data is not None and not data.get("error")
            return ok, data
        except Exception as e:
            self.log.exception("admin_call %s failed", action)
            return False, {"error": str(e)}

    # --------------- Commands ---------------
    @command.new(name="gmail", require_subcommand=True, help="Manage Gmail subscriptions for this room")
    async def gmail(self, evt):
        await self._help(evt)

    @gmail.subcommand(name="help", help="Show help")
    async def _help(self, evt) -> None:
        base = self.config.get("gmail_base_address", "email@example.com") or "email@example.com"
        msg = (
            "**Gmail subscriptions**\n\n"
            "- !gmail sub <alias> <url> [token] — subscribe alias → webhook. If token is omitted, "
            "the the gmail bridge will use URL without auth (or inline auth) \n"
            "- !gmail unsub <alias> — unsubscribe alias\n"
            "- !gmail list — list aliases for this room\n"
            "- !gmail check <alias> — check backend status\n"
            "- !gmail resync — compare local vs backend (by alias)\n"
            "- !gmail perms — debug your permission status here\n\n"
            f"Mails should be sent to: **{base.replace('@', '+<alias>@')}**"
        )
        await self.client.send_markdown(evt.room_id, msg)

    @gmail.subcommand(name="sub", help="Subscribe: !gmail sub <alias> <url> [token]")
    @command.argument("alias", pass_raw=False, required=True)
    @command.argument("url", pass_raw=False, required=True)
    @command.argument("token", pass_raw=False, required=False)
    async def sub(self, evt, alias: str, url: str, token: Optional[str] = None) -> None:
        # Permissions
        if not await self._require_perms(evt.room_id, evt.sender):
            await evt.reply("You are not allowed to do this here.")
            return

        base_alias = self._norm_alias(alias)
        if not base_alias:
            await evt.reply("Invalid alias. Use [a-z0-9._-], max 64.")
            return

        # Append random suffix for uniqueness & unguessability
        final_alias = base_alias
        if bool(self.config.get("alias_append_random", True)):
            import secrets, string
            n = int(self.config.get("alias_random_len", 8) or 8)
            alphabet = string.ascii_lowercase + string.digits
            suffix = "".join(secrets.choice(alphabet) for _ in range(max(1, n)))
            keep = max(1, 64 - (len(suffix) + 1))
            final_alias = base_alias[:keep] + "-" + suffix

        # URL must be https
        if not self._is_https(url):
            await evt.reply("URL must be https://")
            return

        # If token not explicitly given, try to extract from URL (query or hook path)
        clean_url, qtok = self._strip_query_token(url)
        path_tok = self._maybe_token_from_path(clean_url)
        bearer = (token or qtok or path_tok or "")

        # Call backend (expect JSON { ok: true, ... })
        ok, data = await self._admin_call("subscribe", {
            "alias": final_alias,
            "webhook": clean_url,
            "bearer_token": bearer,
        })
        if not ok:
            await evt.reply(f"Backend subscribe failed: {data.get('error') or data}")
            return

        # Persist locally for room UX
        hint = (bearer[-4:] if bearer else None)
        await self.database.execute(
            """
            INSERT INTO email_sub (room_id, alias, webhook, bearer_hint, created_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (room_id, alias)
            DO UPDATE SET webhook=excluded.webhook, bearer_hint=excluded.bearer_hint
            """,
            str(evt.room_id), final_alias, clean_url, hint, self._now_ms(),
        )

        # --- Try to redact the user's command message if it likely contained a token ---
        redacted_note = ""
        token_was_in_command = bool(token or qtok or path_tok)
        if token_was_in_command:
            try:
                await self.client.redact(evt.room_id, evt.event_id, reason="Hide token")
                redacted_note = "\n- token message: **redacted**"
            except Exception:
                # fall through to reminder
                redacted_note = ""

        # Build confirmation (gmail +address)
        addr = self._gmail_address(final_alias)
        tok_status = "set" if bearer else "none"

        backend_echo = "\n- backend: ok" if isinstance(data, dict) and data.get("ok") else ""
        reminder = ""
        if token_was_in_command and not redacted_note:
            reminder = "\n\n⚠️ I couldn't redact your command message. Please delete/redact it manually."

        msg = (
            "✅ **Subscribed**\n\n"
            f"- alias: `{final_alias}`\n"
            f"- send to: `{addr}`\n"
            f"- webhook: `{self._hostish(clean_url)}`\n"
            f"- bearer: **{tok_status}**{(' (…'+hint+')' if hint else '')}"
            f"{backend_echo}"
            f"{redacted_note}"
            f"{reminder}"
        )
        await self.client.send_markdown(evt.room_id, msg)

    @gmail.subcommand(name="unsub", help="Unsubscribe alias: !gmail unsub <alias>")
    @command.argument("alias", required=True)
    async def unsub(self, evt, alias: str) -> None:
        if not await self._require_perms(evt.room_id, evt.sender):
            await evt.reply("You are not allowed to do this here.")
            return
        a = self._norm_alias(alias)
        if not a:
            await evt.reply("Invalid alias.")
            return
        ok, data = await self._admin_call("unsubscribe", {"alias": a})
        if not ok:
            await evt.reply(f"Backend unsubscribe failed: {data.get('error') or data}")
            return
        await self.database.execute(
            "DELETE FROM email_sub WHERE room_id=$1 AND alias=$2",
            str(evt.room_id), a,
        )
        await evt.reply(f"🗑️ Unsubscribed `{a}`.")

    @gmail.subcommand(name="list", help="List aliases in this room")
    async def list_(self, evt) -> None:
        rows = await self.database.fetch(
            "SELECT alias, webhook, bearer_hint FROM email_sub WHERE room_id=$1 ORDER BY alias",
            str(evt.room_id),
        )
        if not rows:
            await evt.reply("No aliases here yet. Use !gmail sub <alias> <url> [token].")
            return
        lines = []
        for r in rows:
            host = self._hostish(r["webhook"]) if r["webhook"] else ""
            bh = r["bearer_hint"]
            lines.append(f"- **{r['alias']}** → `{host}` — bearer: {'set (…'+bh+')' if bh else 'none'}")
        await self.client.send_markdown(evt.room_id, "\n".join(lines))

    @gmail.subcommand(name="check", help="Check backend status for an alias")
    @command.argument("alias", required=True)
    async def check(self, evt, alias: str) -> None:
        a = self._norm_alias(alias)
        if not a:
            await evt.reply("Invalid alias.")
            return
        ok, data = await self._admin_call("check", {"alias": a})
        if not ok:
            await evt.reply(f"Backend check failed: {data.get('error') or data}")
            return
        subscribed = bool(data.get("subscribed"))
        has_bearer = bool(data.get("has_bearer"))
        webhook = data.get("webhook") if self.config.get("show_webhook_in_check", False) else None
        msg = (
            f"**{a}** — subscribed: **{'yes' if subscribed else 'no'}**; "
            f"bearer: **{'set' if has_bearer else 'none'}**"
        )
        if webhook:
            msg += f"; webhook: `{self._hostish(webhook)}`"
        await self.client.send_markdown(evt.room_id, msg)

    @gmail.subcommand(name="resync", help="Compare local aliases vs backend list (names only)")
    async def resync(self, evt) -> None:
        ok, data = await self._admin_call("list")
        if not ok or not data or "subscriptions" not in data:
            await evt.reply(f"Backend list failed: {data.get('error') if isinstance(data, dict) else data}")
            return
        backend_aliases = sorted(list((data.get("subscriptions") or {}).keys()))
        local_rows = await self.database.fetch(
            "SELECT alias FROM email_sub WHERE room_id=$1",
            str(evt.room_id),
        )
        local_aliases = sorted([r["alias"] for r in local_rows])
        only_local = [a for a in local_aliases if a not in backend_aliases]
        only_backend = [a for a in backend_aliases if a not in local_aliases]
        if not only_local and not only_backend:
            await evt.reply("✅ Local matches backend (by alias names).")
            return
        text = ["**Resync report (names only)**"]
        if only_local:
            text.append("- Present locally but not in backend: " + ", ".join(f"`{a}`" for a in only_local))
        if only_backend:
            text.append("- Present in backend but not locally: " + ", ".join(f"`{a}`" for a in only_backend))
        await self.client.send_markdown(evt.room_id, "\n".join(text))

    @gmail.subcommand(name="perms", help="Debug: check your permission status in this room")
    async def perms(self, evt) -> None:
        in_adminlist = evt.sender in set(self.config.get("adminlist", []) or [])
        local_ok = self._is_local(evt.sender)
        required = int(self.config.get("pl_required", 0) or 0)
        level = await self._get_user_pl(evt.room_id, evt.sender)
        allowed = in_adminlist or (local_ok if self.config.get("restrict_commands_to_local", False) else True)
        if required > 0:
            allowed = allowed and (level >= required)
        msg = (
            "**Gmail plugin permission diagnostics**\n"
            f"- You: `{evt.sender}` (server: {self._user_domain(evt.sender)})\n"
            f"- In adminlist: **{'yes' if in_adminlist else 'no'}**\n"
            f"- Local OK (needs {self.config.get('local_homeserver_domain', '')}): **{'yes' if local_ok else 'no'}**\n"
            f"- Required PL: **{required}**\n"
            f"- Your PL in this room: **{level}**\n"
            f"- Result: **{'ALLOWED' if allowed else 'DENIED'}**"
        )
        await self.client.send_markdown(evt.room_id, msg)
