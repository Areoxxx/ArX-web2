import time
import requests
from typing import Callable


BASE_URL = "https://discord.com/api/v9"


class Sender:
    """Discord Bot tokeni ile medya link gönderici."""

    def __init__(self, bot_token: str, storage, log_callback: Callable):
        self.token = bot_token
        self.storage = storage
        self.log = log_callback
        self._running = True
        self._headers = {
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "ArXBot/1.0"
        }

    def set_running(self, value: bool):
        self._running = value

    # ─── Token Doğrulama ──────────────────────────────────────────────────────

    def check_bot_token(self) -> tuple[bool, str]:
        try:
            data = self._make_request("GET", f"{BASE_URL}/users/@me")
            if data and "id" in data:
                name = data.get("username", "Bilinmiyor")
                return True, f"✅ Bot: {name}"
            return False, "❌ Geçersiz bot tokeni."
        except Exception as e:
            return False, f"❌ Hata: {e}"

    # ─── Sunucu Listesi ───────────────────────────────────────────────────────

    def get_bot_guilds(self) -> list:
        """Botun üye olduğu sunucuları döndürür."""
        try:
            data = self._make_request("GET", f"{BASE_URL}/users/@me/guilds")
            return data if isinstance(data, list) else []
        except Exception as e:
            self.log(f"Bot sunucuları alınamadı: {e}", "error")
            return []

    # ─── Kanal Bilgisi ────────────────────────────────────────────────────────

    def get_guild_channels(self, guild_id: str) -> list:
        """
        Bot'un erişebildiği kanal listesi.
        Not: Bot, /users/@me/guilds'i göremez; kullanıcı tokeni üzerinden
        sunucu seçilir, bot tokeni ile kanal listesi alınır.
        """
        try:
            data = self._make_request("GET", f"{BASE_URL}/guilds/{guild_id}/channels")
            return data if isinstance(data, list) else []
        except Exception as e:
            self.log(f"Bot kanalları alınamadı ({guild_id}): {e}", "error")
            return []

    def get_channel_info(self, channel_id: str) -> dict | None:
        try:
            return self._make_request("GET", f"{BASE_URL}/channels/{channel_id}")
        except Exception as e:
            self.log(f"Bot kanal bilgisi alınamadı ({channel_id}): {e}", "error")
            return None

    # ─── Gönderim ─────────────────────────────────────────────────────────────

    def send_media_link(self, channel_id: str, link: str, delay: float = 1.0) -> bool:
        """
        Medya linkini hedef kanala gönderir.
        Başarılı ise True, başarısız ise False döner.
        """
        if not self._running:
            return False

        payload = {"content": link}

        for attempt in range(5):
            if not self._running:
                return False
            try:
                result = self._make_request(
                    "POST",
                    f"{BASE_URL}/channels/{channel_id}/messages",
                    data=payload
                )

                if result and "id" in result:
                    # Başarılı gönderim → SQLite'a kaydet
                    self.storage.save_sent_link(link)
                    time.sleep(delay)
                    return True

                self.log(f"⚠️ Beklenmeyen yanıt: {result}", "warning")
                return False

            except RateLimitError as e:
                self.log(f"⏳ Rate limit! {e.retry_after:.1f}s bekleniyor...", "warning")
                time.sleep(e.retry_after)
                continue

            except PermissionError as e:
                self.log(f"❌ Yetki hatası: {e}", "error")
                return False

            except Exception as e:
                wait = 2 ** attempt
                self.log(f"⚠️ Gönderim hatası (deneme {attempt+1}/5): {e} — {wait}s bekleniyor", "warning")
                time.sleep(wait)

        self.log(f"❌ 5 denemede gönderilemedi: {link[:60]}...", "error")
        return False

    # ─── HTTP Yardımcı ────────────────────────────────────────────────────────

    def _make_request(self, method: str, url: str, data: dict = None) -> any:
        try:
            if method == "GET":
                resp = requests.get(url, headers=self._headers, timeout=15)
            elif method == "POST":
                resp = requests.post(url, headers=self._headers, json=data, timeout=15)
            else:
                raise ValueError(f"Desteklenmeyen method: {method}")

            if resp.status_code in (200, 201):
                return resp.json()

            elif resp.status_code == 204:
                return {}

            elif resp.status_code == 429:
                body = resp.json()
                retry_after = float(body.get("retry_after", 5))
                raise RateLimitError(retry_after)

            elif resp.status_code == 401:
                raise Exception("Yetkisiz (401) - Bot tokeni geçersiz.")

            elif resp.status_code == 403:
                raise PermissionError(f"Bot bu kanala mesaj gönderme yetkisine sahip değil (403).")

            elif resp.status_code == 404:
                raise Exception(f"Kanal bulunamadı (404) - {url}")

            elif resp.status_code >= 500:
                raise Exception(f"Discord sunucu hatası ({resp.status_code})")

            else:
                raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

        except (RateLimitError, PermissionError):
            raise
        except requests.exceptions.Timeout:
            raise Exception("İstek zaman aşımına uğradı.")
        except requests.exceptions.ConnectionError:
            raise Exception("Bağlantı hatası.")


class RateLimitError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limited: {retry_after}s")
