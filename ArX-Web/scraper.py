import time
import requests
from typing import Generator, Callable, Optional


BASE_URL = "https://discord.com/api/v9"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
GIF_EXTS   = (".gif",)
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


class Scraper:
    """Polling tabanlı Discord medya tarayıcısı (Kullanıcı tokeni ile)."""

    def __init__(self, user_token: str, storage, log_callback: Callable):
        self.token = user_token
        self.storage = storage
        self.log = log_callback
        self._running = True
        self._headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

    def set_running(self, value: bool):
        self._running = value

    # ─── Token Doğrulama ──────────────────────────────────────────────────────

    def check_user_token(self) -> tuple[bool, str]:
        try:
            data = self._get_json(f"{BASE_URL}/users/@me")
            if data and "id" in data:
                name = data.get("username", "Bilinmiyor")
                return True, f"✅ Kullanıcı: {name}#{data.get('discriminator','0')}"
            return False, "❌ Geçersiz kullanıcı tokeni."
        except Exception as e:
            return False, f"❌ Hata: {e}"

    # ─── Sunucu & Kanal Bilgisi ───────────────────────────────────────────────

    def get_user_guilds(self) -> list:
        try:
            data = self._get_json(f"{BASE_URL}/users/@me/guilds")
            return data if isinstance(data, list) else []
        except Exception as e:
            self.log(f"Sunucular alınamadı: {e}", "error")
            return []

    def get_guild_channels(self, guild_id: str) -> list:
        try:
            data = self._get_json(f"{BASE_URL}/guilds/{guild_id}/channels")
            return data if isinstance(data, list) else []
        except Exception as e:
            self.log(f"Kanallar alınamadı ({guild_id}): {e}", "error")
            return []

    def get_channel_info(self, channel_id: str) -> Optional[dict]:
        try:
            return self._get_json(f"{BASE_URL}/channels/{channel_id}")
        except Exception as e:
            self.log(f"Kanal bilgisi alınamadı ({channel_id}): {e}", "error")
            return None

    # ─── Ana Tarama (Generator) ───────────────────────────────────────────────

    def scrape_channel(
        self,
        channel_id: str,
        delay: float = 1.0,
        media_limit: Optional[int] = None,
        include_photos: bool = True,
        include_gifs: bool = True,
        progress_callback: Optional[Callable] = None
    ) -> Generator:
        """
        Kanal mesajlarını tarar, medya linklerini anlık yield eder.

        Tarama yönü: en ESKİden en YENİye (after= parametresi ile).
        Devam mantığı:
          - Kanal daha önce hiç taranmadıysa: en baştan başlar.
          - Kanal daha önce yarıda kaldıysa: kaldığı mesaj ID'sinden devam eder.
          - Kanal daha önce tamamen bittiyse: atlanır (skip).
        """
        # ── Daha önce tamamlandıysa atla ──────────────────────────────────────
        progress = self.storage.get_channel_progress(channel_id)
        if progress["completed"]:
            self.log(f"⏭ Kanal {channel_id} zaten tamamlanmış, atlanıyor.", "info")
            return

        # ── Kaldığı yerden devam ──────────────────────────────────────────────
        after_id = progress["last_message_id"]  # None = baştan başla
        if after_id:
            self.log(f"📌 Kanal {channel_id} kaldığı yerden devam ediyor "
                     f"(mesaj ID: {after_id})", "info")
        else:
            self.log(f"🔍 Kanal {channel_id} baştan taranıyor...", "info")

        found_count = 0
        scanned_count = 0

        while self._running:
            # after= ile en eskiden yeniye doğru tarama
            url = f"{BASE_URL}/channels/{channel_id}/messages?limit=100"
            if after_id:
                url += f"&after={after_id}"

            try:
                messages = self._get_json(url)
            except RateLimitError as e:
                self.log(f"⏳ Rate limit! {e.retry_after:.1f}s bekleniyor...", "warning")
                time.sleep(e.retry_after)
                continue
            except Exception as e:
                self.log(f"❌ Mesaj alınamadı (kanal {channel_id}): {e}", "error")
                # Progress'i koru, kapanınca kaldığı yerden devam etsin
                return

            if not messages or not isinstance(messages, list) or len(messages) == 0:
                # Kanal tamamen bitti
                self.log(f"✅ Kanal {channel_id} tamamen tarandı "
                         f"({scanned_count} mesaj, {found_count} medya).", "info")
                self.storage.mark_channel_completed(channel_id)
                return

            # after= parametresi en eskiyi en başa koyar (ID artan sıra)
            # Yine de garantilemek için sırala: küçük ID → eski, büyük ID → yeni
            messages.sort(key=lambda m: int(m.get("id", 0)))

            for msg in messages:
                if not self._running:
                    # Durdu → progress zaten kaydedildi, return
                    return

                scanned_count += 1
                msg_id = msg.get("id", "")

                links = self._extract_media_links(msg, include_photos, include_gifs)

                for link in links:
                    if media_limit and found_count >= media_limit:
                        self.log(f"🎯 Medya limiti ({media_limit}) doldu.", "info")
                        return

                    if not self.storage.is_link_sent(link):
                        yield (msg_id, link)
                        found_count += 1

                        if progress_callback:
                            progress_callback(scanned=scanned_count, found=found_count)

                # Her mesajdan sonra after_id'yi güncelle ve kaydet
                # Bu sayede herhangi bir noktada kapanırsa kaldığı yerden devam eder
                if msg_id:
                    after_id = msg_id
                    self.storage.save_channel_progress(
                        channel_id, after_id, completed=False
                    )

                time.sleep(delay * 0.02)  # Tarama hızı (gönderim delay'inden bağımsız)

            # 100'den az mesaj geldi → kanal bitti
            if len(messages) < 100:
                self.log(f"✅ Kanal {channel_id} tamamen tarandı "
                         f"({scanned_count} mesaj, {found_count} medya).", "info")
                self.storage.mark_channel_completed(channel_id)
                return

    # ─── Medya Link Çıkarma ───────────────────────────────────────────────────

    def _extract_media_links(self, message: dict, include_photos=True, include_gifs=True) -> list:
        links = []

        # Attachments
        for attachment in message.get("attachments", []):
            url = attachment.get("url", "")
            if not url:
                continue

            url_lower = url.lower().split("?")[0]

            if url_lower.endswith(VIDEO_EXTS):
                links.append(url)
            elif include_photos and url_lower.endswith(IMAGE_EXTS):
                links.append(url)
            elif include_gifs and url_lower.endswith(GIF_EXTS):
                links.append(url)

        # Embeds (video/gif embeds)
        for embed in message.get("embeds", []):
            embed_type = embed.get("type", "")

            if embed_type == "video":
                video = embed.get("video", {})
                url = video.get("url", "")
                if url:
                    links.append(url)

            elif embed_type == "gifv" and include_gifs:
                video = embed.get("video", {})
                url = video.get("url", "")
                if url:
                    links.append(url)

            elif embed_type == "image" and include_photos:
                thumbnail = embed.get("thumbnail", {})
                url = thumbnail.get("url", "")
                if url:
                    links.append(url)

        return links

    # ─── HTTP Yardımcı ────────────────────────────────────────────────────────

    def _get_json(self, url: str) -> any:
        for attempt in range(5):
            try:
                resp = requests.get(url, headers=self._headers, timeout=15)

                if resp.status_code == 200:
                    return resp.json()

                elif resp.status_code == 429:
                    retry_after = float(resp.json().get("retry_after", 5))
                    raise RateLimitError(retry_after)

                elif resp.status_code == 401:
                    raise Exception("Yetkisiz erişim (401) — Token geçersiz veya süresi dolmuş.")

                elif resp.status_code == 403:
                    raise Exception(f"Erişim reddedildi (403) — {url}")

                elif resp.status_code == 404:
                    raise Exception(f"Bulunamadı (404) — {url}")

                elif resp.status_code >= 500:
                    wait = 2 ** attempt
                    self.log(f"⚠️ Discord sunucu hatası ({resp.status_code}), {wait}s sonra tekrar...", "warning")
                    time.sleep(wait)
                    continue

                else:
                    raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

            except RateLimitError:
                raise
            except requests.exceptions.Timeout:
                wait = 2 ** attempt
                self.log(f"⚠️ Zaman aşımı, {wait}s sonra tekrar deneniyor...", "warning")
                time.sleep(wait)
            except requests.exceptions.ConnectionError:
                wait = 3 * (attempt + 1)
                self.log(f"⚠️ Bağlantı hatası, {wait}s sonra tekrar deneniyor...", "warning")
                time.sleep(wait)

        raise Exception(f"5 denemede başarısız: {url}")


class RateLimitError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limited: {retry_after}s")
