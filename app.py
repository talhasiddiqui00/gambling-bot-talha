# """
# Highrise Blackjack Gambling Bot (blackjack_bot.py)
# Standalone bot - Blackjack rounds only. No emotes, VIP, DJ, or trivia.

# Round flow:
#   1. "New round in 1 min" announcement
#   2. 20s betting window (tip gold to bet) with 10s/5s warnings
#   3. Betting closes - late tips are refunded, not counted
#   4. 1s grace, then "rolling the cards..." + 2s delay
#   5. Real per-player turns: each bettor is dealt 2 cards and can type
#      !hit / !stand on their own turn (20s per turn, auto-stand on timeout)
#   6. Dealer reveals hole card and auto-plays (hits while under 17)
#   7. Results + payouts announced, winners tipped
#   8. Round data cleared, next round begins

# Payouts: standard 2x on a win, 2.5x on a natural Blackjack, push returns
# the exact bet, loss forfeits the bet. Real random cards are dealt every
# round - nothing is rigged. A house exposure cap limits how much total
# betting is accepted in a single round relative to the bot's own gold
# balance, so one unlucky round can't wipe it out - this is honest risk
# management, not manipulating outcomes.
# """

import os
import sys
import time
import math
import random
import asyncio
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from json import load, dump

from highrise import BaseBot, User, Position, AnchorPosition, SessionMetadata, CurrencyItem, Item
from highrise.__main__ import main, BotDefinition

sys.stdout.reconfigure(line_buffering=True)
os.environ["PYTHONUNBUFFERED"] = "1"

# --- ROOM / BOT CREDENTIALS ---
# ROOM_ID is the same room as your main bot.
# WARNING: the API_TOKEN below is a placeholder built from the value you gave me.
# It's only 24 hex characters, the same shape as a Room ID / object id - Highrise
# bot API tokens are normally much longer (60+ characters, like your main bot's
# token). Double-check this on Highrise's bot settings page and replace it below,
# or the bot won't be able to connect at all.
ROOM_ID = "6a28b5b000b6151bd4c9641e"
API_TOKEN = "ca3eb4565417e356e291ea4832d8df1422365d5fa2aa528827cba5bc55655a04"  # <-- VERIFY/REPLACE THIS

DATA_FILE = "./blackjack_data.json"

# --- Optional permanent storage via GitHub Gist (same pattern as your main bot) ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GIST_ID = os.environ.get("GIST_ID", "").strip()
GIST_FILENAME = "blackjack_data.json"

# Gold-bar tip denominations available on Highrise, used both to accept bets
# and to pay out winnings (payouts are decomposed into these exact bars).
TIP_MAP = {
    "1g": "gold_bar_1", "5g": "gold_bar_5", "10g": "gold_bar_10",
    "50g": "gold_bar_50", "100g": "gold_bar_100", "500g": "gold_bar_500",
    "1k": "gold_bar_1k", "5k": "gold_bar_5k", "10k": "gold_bar_10k",
}
# Ordered largest-to-smallest so payouts use the fewest possible bars.
DENOMINATION_VALUES = [
    (10000, "10k"), (5000, "5k"), (1000, "1k"), (500, "500g"),
    (100, "100g"), (50, "50g"), (10, "10g"), (5, "5g"), (1, "1g"),
]

WIN_MULTIPLIER = 2.0
BLACKJACK_MULTIPLIER = 2.5
BET_WINDOW_SECONDS = 20
PLAYER_TURN_SECONDS = 20
# The bot won't accept bets in a round whose worst-case total payout (all
# bets paying out at the Blackjack rate) would exceed this fraction of its
# current gold balance. This is a real safety cap, not a rigged deck.
MAX_EXPOSURE_FRACTION = 0.5

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUES = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 10, "Q": 10, "K": 10, "A": 11,
}

RULES_TEXT = (
    "🃏 <color=#FFD700><b>BLACKJACK RULES</b></color> 🃏\n"
    "💰 Tip ANY amount of gold to the bot during the betting window - that's your bet!\n"
    "🂡 You'll get 2 cards. Type <b>!hit</b> to draw another, or <b>!stand</b> to hold.\n"
    "🎯 Get as close to 21 as you can WITHOUT going over - go over 21 and you bust (lose instantly).\n"
    "👑 Beat the dealer's final hand to win <color=#FFD700><b>2x</b></color> your bet!\n"
    "🎉 A natural Blackjack (21 with your first 2 cards) pays <color=#FFD700><b>2.5x</b></color>!\n"
    "🤝 Tie the dealer = push, your bet is returned.\n"
    "⏱️ You only have a short time on your turn - no response means you auto-stand!"
)


def build_shuffled_deck() -> list:
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def format_card(card: tuple) -> str:
    return f"{card[0]}{card[1]}"


def format_hand(cards: list) -> str:
    return " ".join(format_card(c) for c in cards)


def hand_value(cards: list) -> int:
    total = sum(RANK_VALUES[r] for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def decompose_amount(amount: int) -> list:
    # Greedy breakdown into available gold-bar denominations. Since "1g" is
    # always available, any non-negative integer amount can be represented.
    parts = []
    remaining = int(amount)
    for value, key in DENOMINATION_VALUES:
        while remaining >= value:
            parts.append(key)
            remaining -= value
    return parts


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Blackjack Bot Live")

    def log_message(self, format, *args):
        return


class Bot(BaseBot):
    instance = None

    def __init__(self):
        super().__init__()
        Bot.instance = self
        self.bot_id = None
        self.owner_username = "sadi_key"
        self.is_initialized = False
        self.last_command_time = {}

        self.betting_open = False
        self.current_bets = {}       # user_id -> {"username", "amount", "cards", "done", "busted"}
        self.active_turn_user_id = None
        self.wallet_cache_gold = 0
        self.current_deck = []       # shared shuffled deck for the active round - !hit draws from this
        self.tip_queue = asyncio.Queue()

        self._gist_dirty = False
        self._gist_pending_data = None

        self._load_startup_state()

    # --- persistence (same pattern as the main bot) ---

    def _gist_configured(self) -> bool:
        return bool(GITHUB_TOKEN and GIST_ID)

    def _gist_headers(self) -> dict:
        return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

    def fetch_gist_data(self):
        try:
            resp = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=self._gist_headers(), timeout=10)
            resp.raise_for_status()
            file_entry = resp.json().get("files", {}).get(GIST_FILENAME)
            if not file_entry:
                return None
            content = file_entry.get("content", "")
            if file_entry.get("truncated") and file_entry.get("raw_url"):
                raw = requests.get(file_entry["raw_url"], timeout=10)
                raw.raise_for_status()
                content = raw.text
            return json.loads(content) if content.strip() else None
        except Exception as e:
            print(f"[GIST ERROR] Fetch failed, falling back to local disk: {e}")
            return None

    def push_gist_data(self, data: dict) -> None:
        try:
            payload = {"files": {GIST_FILENAME: {"content": json.dumps(data, indent=4)}}}
            resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=self._gist_headers(), json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"[GIST ERROR] Push failed (safe on local disk this run): {e}")

    def queue_gist_push(self, data: dict) -> None:
        self._gist_pending_data = data
        self._gist_dirty = True

    async def gist_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            if not self._gist_configured() or not self._gist_dirty:
                continue
            data_to_push = self._gist_pending_data
            self._gist_dirty = False
            try:
                await asyncio.to_thread(self.push_gist_data, data_to_push)
            except Exception as e:
                print(f"[GIST ERROR] Sync loop push failed, will retry: {e}")
                self._gist_pending_data = data_to_push
                self._gist_dirty = True

    def _load_startup_state(self) -> None:
        # Recover the bot's saved position, and refund anyone whose bet was
        # left stranded by a crash/redeploy mid-round (fairness safety net).
        data = None
        if self._gist_configured():
            data = self.fetch_gist_data()
        if data is None and os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    data = load(f)
            except Exception:
                data = None
        data = data or {}
        self._saved_bot_position = data.get("bot_position", {"x": 0, "y": 0, "z": 0, "facing": "FrontRight"})
        self._stranded_bets = data.get("pending_bets", {}) or {}
        self._save_state(pending_bets={})  # clear on disk immediately; we'll refund in on_start

    def _save_state(self, pending_bets: dict = None) -> None:
        try:
            data = {}
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, "r") as f:
                        data = load(f)
                except Exception:
                    data = {}
            if pending_bets is not None:
                data["pending_bets"] = pending_bets
            data.setdefault("bot_position", getattr(self, "_saved_bot_position", {"x": 0, "y": 0, "z": 0, "facing": "FrontRight"}))
            with open(DATA_FILE, "w") as f:
                dump(data, f, indent=4)
            if self._gist_configured():
                self.queue_gist_push(data)
        except Exception as e:
            print(f"[STATE ERROR] Save failed: {e}")

    def get_bot_position(self) -> Position:
        pos = getattr(self, "_saved_bot_position", {"x": 0, "y": 0, "z": 0, "facing": "FrontRight"})
        return Position(pos["x"], pos["y"], pos["z"], pos["facing"])

    # --- tip payout queue (silent - round summary announces results, not each bar) ---

    async def process_tip_queue_worker(self):
        while True:
            target_id, gold_bar_tier, username, reason = await self.tip_queue.get()
            try:
                await self.highrise.tip_user(target_id, gold_bar_tier)
                await asyncio.sleep(random.uniform(1.5, 2.5))
            except Exception as e:
                print(f"[TIP ERROR] Failed to pay {username} ({target_id}): {e}")
                await asyncio.sleep(1.0)
            finally:
                self.tip_queue.task_done()

    async def queue_payout(self, user_id: str, username: str, amount: int, reason: str) -> None:
        for part in decompose_amount(amount):
            await self.tip_queue.put((user_id, TIP_MAP[part], username, reason))

    async def connection_watchdog_loop(self) -> None:
        consecutive_failures = 0
        while True:
            await asyncio.sleep(45)
            try:
                await self.highrise.get_wallet()
                consecutive_failures = 0
            except Exception as e:
                if "closing transport" in str(e).lower() or "timeout" in str(e).lower():
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print("[WATCHDOG] Connection appears dead - restarting process.")
                        os._exit(1)

    # --- main game loop ---

    async def announce(self, msg: str) -> None:
        try:
            await self.highrise.chat(msg)
        except Exception as e:
            print(f"[ANNOUNCE ERROR] {e}")

    async def get_wallet_gold(self):
        # Returns None on a failed fetch (so callers know not to trust it),
        # and an actual integer - including a real 0 - when the fetch worked.
        try:
            wallet = await self.highrise.get_wallet()
            return next((c.amount for c in wallet.content if c.type == "gold"), 0)
        except Exception:
            return None

    async def blackjack_loop(self) -> None:
        # Refund any bets that were stranded by a crash before this instance started.
        if getattr(self, "_stranded_bets", None):
            for uid, info in self._stranded_bets.items():
                await self.queue_payout(uid, info.get("username", "player"), info.get("amount", 0), "bj_refund_recovery")
            self._stranded_bets = {}

        while True:
            await self.announce("🎰 A new <color=#FFD700><b>Blackjack</b></color> round starts in <b>1 min</b>! Get your gold ready! 🃏")
            await asyncio.sleep(60)

            self.current_bets = {}
            self.betting_open = True
            fetched_gold = await self.get_wallet_gold()
            self.wallet_cache_gold = fetched_gold if fetched_gold is not None else 0
            self._save_state(pending_bets={})

            await self.announce(
                RULES_TEXT + "\n\n💰 <color=#00FF00><b>BETTING IS OPEN for 20 seconds!</b></color> "
                "Tip any gold amount to the bot right now to place your bet!"
            )

            start = asyncio.get_running_loop().time()
            warned = set()
            while True:
                left = BET_WINDOW_SECONDS - (asyncio.get_running_loop().time() - start)
                if left <= 0:
                    break
                if left <= 10 and 10 not in warned:
                    warned.add(10)
                    await self.announce("⏳ <b>10 seconds</b> left to place your bet!")
                if left <= 5 and 5 not in warned:
                    warned.add(5)
                    await self.announce("⏳ <b>5 seconds</b> left - last chance to bet!")
                await asyncio.sleep(min(1, max(0.1, left)))

            self.betting_open = False
            await self.announce(
                "⛔ <color=#FF0000><b>BETTING IS CLOSED!</b></color> Any tips from now on will NOT count "
                "for this round - they'll be refunded, so hang on to your gold for the next one!"
            )
            await asyncio.sleep(1)

            if not self.current_bets:
                await self.announce("😴 Nobody placed a bet this round - skipping to the next one!")
                continue

            await self.announce("🎴 Rolling the cards...")
            await asyncio.sleep(2)

            deck = build_shuffled_deck()
            self.current_deck = deck
            dealer_cards = [deck.pop(), deck.pop()]
            for info in self.current_bets.values():
                info["cards"] = [deck.pop(), deck.pop()]
                info["done"] = False
                info["busted"] = False

            dealer_up = dealer_cards[0]
            await self.announce(f"🂠 Dealer shows: <b>{format_card(dealer_up)}</b> and a hidden card.")

            # Sequential per-player turns
            for uid, info in list(self.current_bets.items()):
                username = info["username"]
                cards = info["cards"]

                if is_blackjack(cards):
                    await self.announce(f"🎉 @{username} has a natural <color=#FFD700><b>BLACKJACK</b></color>! {format_hand(cards)} (21)! Auto-standing.")
                    info["done"] = True
                    continue

                self.active_turn_user_id = uid
                await self.announce(
                    f"👉 @{username}'s turn! Your hand: <b>{format_hand(cards)}</b> ({hand_value(cards)}). "
                    f"Dealer shows {format_card(dealer_up)}. Type <b>!hit</b> or <b>!stand</b>! (20s)"
                )
                turn_start = asyncio.get_running_loop().time()
                while not info["done"]:
                    if asyncio.get_running_loop().time() - turn_start > PLAYER_TURN_SECONDS:
                        info["done"] = True
                        await self.announce(f"⌛ @{username} ran out of time - auto-standing with {hand_value(cards)}.")
                        break
                    await asyncio.sleep(1)
                self.active_turn_user_id = None

            # Dealer's turn
            await self.announce(f"🂠 Dealer reveals: <b>{format_hand(dealer_cards)}</b> ({hand_value(dealer_cards)})")
            while hand_value(dealer_cards) < 17:
                dealer_cards.append(deck.pop())
            dealer_total = hand_value(dealer_cards)
            dealer_bust = dealer_total > 21
            dealer_bj = is_blackjack(dealer_cards)
            await self.announce(
                f"🂠 Dealer's final hand: <b>{format_hand(dealer_cards)}</b> ({dealer_total})"
                + (" - <color=#FF0000><b>BUST!</b></color>" if dealer_bust else "")
            )

            # Resolve each bet
            result_lines = []
            for uid, info in self.current_bets.items():
                username = info["username"]
                bet = info["amount"]
                cards = info["cards"]
                player_total = hand_value(cards)
                player_bust = player_total > 21
                player_bj = is_blackjack(cards)

                if player_bust:
                    outcome = "lose"
                elif player_bj and dealer_bj:
                    outcome = "push"
                elif player_bj:
                    outcome = "blackjack"
                elif dealer_bust:
                    outcome = "win"
                elif player_total > dealer_total:
                    outcome = "win"
                elif player_total == dealer_total:
                    outcome = "push"
                else:
                    outcome = "lose"

                if outcome == "win":
                    payout = math.floor(bet * WIN_MULTIPLIER)
                    await self.queue_payout(uid, username, payout, "bj_win")
                    result_lines.append(f"🎉 @{username} WINS <color=#FFD700><b>{payout}g</b></color>! (bet {bet}g, hand {player_total})")
                elif outcome == "blackjack":
                    payout = math.floor(bet * BLACKJACK_MULTIPLIER)
                    await self.queue_payout(uid, username, payout, "bj_blackjack")
                    result_lines.append(f"🃏 @{username} BLACKJACK! Wins <color=#FFD700><b>{payout}g</b></color>! (bet {bet}g)")
                elif outcome == "push":
                    await self.queue_payout(uid, username, bet, "bj_push")
                    result_lines.append(f"🤝 @{username} PUSH - {bet}g bet refunded. (hand {player_total})")
                else:
                    result_lines.append(f"💀 @{username} loses {bet}g. (hand {player_total})")

            await self.announce("\n".join(result_lines))
            await self.announce("🏁 Round over! Next round starting soon...")

            self.current_bets = {}
            self._save_state(pending_bets={})

    # --- Highrise event hooks ---

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        print("Blackjack Bot Connected")
        self.bot_id = session_metadata.user_id
        asyncio.create_task(self.place_bot())

        if self.is_initialized:
            return
        self.is_initialized = True

        asyncio.create_task(self.process_tip_queue_worker())
        asyncio.create_task(self.connection_watchdog_loop())
        asyncio.create_task(self.gist_sync_loop())
        asyncio.create_task(self.blackjack_loop())

    async def place_bot(self):
        await asyncio.sleep(2.0)
        pos = self.get_bot_position()
        if pos == Position(0, 0, 0, "FrontRight"):
            return
        for _ in range(5):
            try:
                await self.highrise.teleport(self.bot_id, pos)
                return
            except Exception:
                await asyncio.sleep(2.0)

    async def on_tip(self, sender: User, receiver: User, tip) -> None:
        if sender.id == self.bot_id or receiver.id != self.bot_id:
            return
        if not isinstance(tip, CurrencyItem):
            return

        if not self.betting_open:
            # Round is closed (or hasn't opened) - refund, don't silently keep it.
            await self.queue_payout(sender.id, sender.username, tip.amount, "bj_late_refund")
            try:
                await self.highrise.send_whisper(
                    sender.id,
                    f"⏰ Betting is closed right now, so your {tip.amount}g tip didn't count as a bet - "
                    "it's being refunded. Watch chat for when the next round opens!"
                )
            except Exception:
                pass
            return

        existing = self.current_bets.get(sender.id)
        current_total_bets = sum(b["amount"] for b in self.current_bets.values())
        worst_case_if_added = (current_total_bets + tip.amount) * BLACKJACK_MULTIPLIER
        cap = self.wallet_cache_gold * MAX_EXPOSURE_FRACTION

        if worst_case_if_added > cap:
            await self.queue_payout(sender.id, sender.username, tip.amount, "bj_cap_refund")
            try:
                await self.highrise.send_whisper(
                    sender.id,
                    f"⚠️ The house's betting limit is reached for this round - your {tip.amount}g tip is being "
                    "refunded. Try a smaller bet or catch the next round!"
                )
            except Exception:
                pass
            return

        if existing:
            existing["amount"] += tip.amount
            new_total = existing["amount"]
        else:
            self.current_bets[sender.id] = {"username": sender.username, "amount": tip.amount, "cards": [], "done": False, "busted": False}
            new_total = tip.amount

        self._save_state(pending_bets={uid: {"username": b["username"], "amount": b["amount"]} for uid, b in self.current_bets.items()})

        try:
            await self.highrise.send_whisper(sender.id, f"✅ You now have <b>{new_total}g</b> bet on this round's Blackjack! Good luck! 🍀")
        except Exception:
            pass

    async def on_chat(self, user: User, message: str) -> None:
        await self.command_handler(user, message, "chat")

    async def on_whisper(self, user: User, message: str) -> None:
        await self.command_handler(user, message, "whisper")

    async def respond(self, user: User, msg: str, source: str):
        if source == "chat":
            await self.highrise.chat(msg)
        else:
            await self.highrise.send_whisper(user.id, msg)

    async def command_handler(self, user: User, message: str, source: str):
        if not message or not message.strip():
            return
        clean_msg = message.lower().strip()

        now = time.time()
        user_history = self.last_command_time.get(user.id, {})
        last_time = user_history.get(clean_msg, 0)
        if now - last_time < 1.5:
            return
        user_history[clean_msg] = now
        self.last_command_time[user.id] = user_history

        is_owner = user.username.lower() == self.owner_username.lower()

        # !hit / !stand - ONLY the player whose turn it currently is gets heard.
        if clean_msg in ("!hit", "!stand"):
            if user.id != self.active_turn_user_id:
                return  # Not your turn - silently ignored, no spam.
            info = self.current_bets.get(user.id)
            if not info:
                return
            if clean_msg == "!stand":
                info["done"] = True
                await self.respond(user, f"✋ @{user.username} stands with {hand_value(info['cards'])}.", "chat")
                return
            # !hit
            if not self.current_deck:
                # Extremely unlikely (would need ~26 hits in one round), but
                # top up with a fresh shuffled deck rather than crashing.
                self.current_deck = build_shuffled_deck()
            info["cards"].append(self.current_deck.pop())
            total = hand_value(info["cards"])
            if total > 21:
                info["busted"] = True
                info["done"] = True
                await self.respond(user, f"💥 @{user.username} draws {format_card(info['cards'][-1])} - hand: {format_hand(info['cards'])} ({total}) - BUST!", "chat")
            else:
                await self.respond(user, f"🂠 @{user.username} draws {format_card(info['cards'][-1])} - hand: {format_hand(info['cards'])} ({total}). Hit or stand?", "chat")
            return

        if clean_msg == "!rules":
            await self.respond(user, RULES_TEXT, source)
            return

        if not is_owner:
            return

        if clean_msg == "!gset":
            try:
                room_users = await self.highrise.get_room_users()
                position = None
                for u, pos in room_users.content:
                    if u.id == user.id:
                        position = pos
                        break
                if isinstance(position, Position):
                    self._saved_bot_position = {"x": position.x, "y": position.y, "z": position.z, "facing": position.facing}
                    self._save_state()
                    await self.highrise.teleport(self.bot_id, position)
                    await self.respond(user, "📍 Bot position updated successfully!", source)
            except Exception as e:
                print(f"[SET ERROR] {e}")
            return

        if clean_msg == "!gbal":
            gold = await self.get_wallet_gold()
            if gold is None:
                await self.highrise.send_whisper(user.id, "⚠️ Couldn't fetch the wallet balance right now - try again in a moment.")
            else:
                await self.highrise.send_whisper(user.id, f"💰 Blackjack bot balance: {gold}g")
            return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


if __name__ == "__main__":
    web_worker = threading.Thread(target=run_web_server, daemon=True)
    web_worker.start()
    asyncio.run(main([BotDefinition(Bot(), ROOM_ID, API_TOKEN)]))
