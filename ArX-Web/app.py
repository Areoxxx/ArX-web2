"""
ArX V2 — Streamlit Web Dashboard (Düzeltilmiş)
"""

import gc
import json
import threading
import time
from collections import deque
from datetime import datetime

import streamlit as st

from scraper import Scraper
from sender import Sender
from storage import Storage

st.set_page_config(page_title="ArX", page_icon="🟣", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #0D0D12; }
[data-testid="stSidebar"] { background: #12121A; }
.stat-card { background:#16161F; border:1px solid #8A2BE240; border-radius:8px;
             padding:12px 16px; margin:4px 0; text-align:center; }
.stat-val  { font-size:24px; font-weight:bold; color:#8A2BE2; }
.stat-lbl  { font-size:11px; color:#888; margin-top:2px; }
.log-box   { background:#080810; border:1px solid #1a1a2e; border-radius:6px;
             padding:10px; height:320px; overflow-y:auto;
             font-family:monospace; font-size:11px; }
.li { color:#ccc; } .ls { color:#2ECC71; }
.lw { color:#FFA040; } .le { color:#FF6B6B; }
.arx { font-size:26px; font-weight:bold; color:white; }
.arx b { color:#8A2BE2; }
</style>
""", unsafe_allow_html=True)

DB_PATH = "/tmp/arx_data.db"

# Thread-safe paylaşımlı state (thread session_state'e dokunamaz)
_shared = {
    "running":  False,
    "paused":   False,
    "stats":    {"found": 0, "sent": 0, "active": "—"},
    "progress": 0.0,
    "logs":     deque(maxlen=300),
}
_lock = threading.Lock()

def _log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _shared["logs"].appendleft({"ts": ts, "msg": msg, "level": level})

def _init():
    defs = {
        "storage": None, "scraper": None, "sender": None,
        "mapping": {}, "src_channels": [], "tgt_channels": [],
        "user_guilds": [], "bot_guilds": [],
        "token_ok": False, "token_msg": "",
        "_utok": "", "_btok": "",
    }
    for k, v in defs.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

if st.session_state.storage is None:
    st.session_state.storage = Storage(DB_PATH)
storage = st.session_state.storage

# ── Transfer worker ────────────────────────────────────────────────────────────
def _worker(scraper, sender, mapping, delay, mlimit, ph, gif, vid, rr):
    valid = [(s, t) for s, t in mapping.items() if t]
    if not valid:
        _log("❌ Eşlenmiş kanal yok!", "error")
        with _lock: _shared["running"] = False
        return

    total = len(valid)
    _log(f"🚀 Başladı — {total} çift", "success")

    try:
        if rr:
            gens = {s: scraper.scrape_channel(s, delay, mlimit, ph, gif, vid) for s, _ in valid}
            done = set()
            while _shared["running"] and len(done) < total:
                ok = False
                for s, t in valid:
                    if s in done or not _shared["running"]: continue
                    while _shared["paused"] and _shared["running"]: time.sleep(0.3)
                    try:
                        _, link = next(gens[s])
                        with _lock: _shared["stats"]["found"] += 1
                        if sender.send_media_link(t, link, delay):
                            with _lock:
                                _shared["stats"]["sent"] += 1
                            _log(f"✅ RR {link[:55]}...", "success")
                        ok = True
                    except StopIteration:
                        done.add(s)
                        _log(f"✅ Bitti: {s}", "success")
                if not ok: break
        else:
            saved = int(storage.get_session_value("active_pair_index", "0"))
            for idx, (s, t) in enumerate(valid):
                if not _shared["running"]: break
                if idx < saved: continue
                storage.save_session_value("active_pair_index", str(idx))
                with _lock: _shared["stats"]["active"] = f"{s} → {t}"
                _log(f"📂 [{idx+1}/{total}] {s} → {t}", "info")
                for _, link in scraper.scrape_channel(s, delay, mlimit, ph, gif, vid):
                    while _shared["paused"] and _shared["running"]: time.sleep(0.3)
                    if not _shared["running"]: break
                    with _lock: _shared["stats"]["found"] += 1
                    if sender.send_media_link(t, link, delay):
                        with _lock:
                            _shared["stats"]["sent"] += 1
                            sent = _shared["stats"]["sent"]
                        _log(f"✅ ({sent}) {link[:60]}...", "success")
                    else:
                        _log(f"⚠️ Gönderilemedi: {link[:55]}...", "warning")
                    with _lock:
                        _shared["progress"] = (idx + _shared["stats"]["sent"] /
                                               max(_shared["stats"]["found"], 1)) / total
                gc.collect()
                if _shared["running"]:
                    storage.save_session_value("active_pair_index", str(idx + 1))
                    _log(f"✅ Kanal tamamlandı: {s}", "success")
            if _shared["running"]:
                storage.clear_session_value("active_pair_index")
                with _lock: _shared["progress"] = 1.0
                _log("🎉 Tamamlandı!", "success")
    except Exception as e:
        _log(f"❌ Hata: {e}", "error")
    finally:
        with _lock: _shared["running"] = False

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<div class="arx">Ar<b>X</b></div>', unsafe_allow_html=True)
    st.caption("Developer areoxzx_")
    st.divider()

    st.subheader("🔑 Tokenler")
    u_tok = st.text_input("Kullanıcı Tokeni", type="password", key="inp_ut",
                           value=st.session_state._utok)
    b_tok = st.text_input("Bot Tokeni",       type="password", key="inp_bt",
                           value=st.session_state._btok)

    if st.button("✅ Kontrol Et", use_container_width=True):
        if u_tok and b_tok:
            with st.spinner("Kontrol..."):
                st.session_state.scraper = Scraper(u_tok, storage, _log)
                st.session_state.sender  = Sender(b_tok,  storage, _log)
                u_ok, u_msg = st.session_state.scraper.check_user_token()
                b_ok, b_msg = st.session_state.sender.check_bot_token()
                st.session_state.token_ok  = u_ok and b_ok
                st.session_state.token_msg = f"{u_msg}\n{b_msg}"
                st.session_state._utok = u_tok
                st.session_state._btok = b_tok
                if st.session_state.token_ok:
                    st.session_state.user_guilds = st.session_state.scraper.get_user_guilds()
                    st.session_state.bot_guilds  = st.session_state.sender.get_bot_guilds()
                    _log("✅ Tokenler doğrulandı.", "success")
                else:
                    _log("❌ Token hatası.", "error")
                st.rerun()
        else:
            st.warning("Her iki tokeni de gir.")

    if st.session_state.token_msg:
        c = "green" if st.session_state.token_ok else "red"
        for line in st.session_state.token_msg.split("\n"):
            if line.strip(): st.markdown(f":{c}[{line}]")

    st.divider()
    st.subheader("⚙️ Ayarlar")
    delay    = st.select_slider("Gecikme", [1,2,5,10], value=1, format_func=lambda x: f"{x}s")
    lraw     = st.text_input("Limit (boş=∞)", key="inp_lim")
    mlimit   = int(lraw) if lraw.strip().isdigit() else None
    inc_ph   = st.checkbox("📷 Fotoğraf", True)
    inc_gif  = st.checkbox("🎞 GIF",      True)
    inc_vid  = st.checkbox("🎬 Video",    True)
    rr_mode  = st.checkbox("🔄 Tane Tane Gönder", False)

    st.divider()
    st.subheader("🎮 Kontrol")
    is_run = _shared["running"]
    is_pau = _shared["paused"]

    c1, c2 = st.columns(2)
    with c1:
        if st.button("▶ Başlat", use_container_width=True,
                     type="primary", disabled=is_run):
            mapping = st.session_state.mapping
            if not mapping:
                mapping = storage.get_channel_mapping()
                if mapping:
                    st.session_state.mapping = mapping
                else:
                    st.error("Kanal eşlemesi yok!")
                    st.stop()
            valid = {s: t for s, t in mapping.items() if t}
            if not valid:
                st.error("Hiçbir kanala hedef atanmamış!")
            elif not st.session_state.scraper:
                st.error("Tokenları kontrol et!")
            else:
                with _lock:
                    _shared.update({"running": True, "paused": False,
                                    "progress": 0.0,
                                    "stats": {"found":0,"sent":0,"active":"Başlıyor..."}})
                st.session_state.scraper.set_running(True)
                st.session_state.sender.set_running(True)
                storage.save_channel_mapping(mapping)
                threading.Thread(
                    target=_worker,
                    args=(st.session_state.scraper, st.session_state.sender,
                          mapping, delay, mlimit, inc_ph, inc_gif, inc_vid, rr_mode),
                    daemon=True
                ).start()
                st.rerun()
    with c2:
        if st.button("■ Durdur", use_container_width=True, disabled=not is_run):
            with _lock: _shared["running"] = False
            if st.session_state.scraper: st.session_state.scraper.set_running(False)
            if st.session_state.sender:  st.session_state.sender.set_running(False)
            _log("⏹ Durduruldu.", "warning")
            st.rerun()

    plbl = "▶ Devam" if is_pau else "⏸ Duraklat"
    if st.button(plbl, use_container_width=True, disabled=not is_run):
        with _lock: _shared["paused"] = not _shared["paused"]
        st.rerun()

    st.divider()
    st.subheader("💾 Config")

    # Config indir — her zaman görünür, buton gerekmez
    cfg_json = json.dumps({
        "tokens":   {"user_token": st.session_state._utok, "bot_token": st.session_state._btok},
        "settings": {"delay": delay, "media_limit": lraw, "include_photos": inc_ph,
                     "include_gifs": inc_gif, "include_videos": inc_vid, "round_robin": rr_mode},
        "mapping":  st.session_state.mapping,
        "src_channels": [{"id": c["id"], "name": c["name"]} for c in st.session_state.src_channels],
        "tgt_channels": [{"id": c["id"], "name": c["name"]} for c in st.session_state.tgt_channels],
    }, ensure_ascii=False, indent=2)

    st.download_button("💾 Config İndir", data=cfg_json,
                       file_name="arx_config.json", mime="application/json",
                       use_container_width=True)

    up = st.file_uploader("📂 Config Yükle", type="json",
                           label_visibility="collapsed", key="fup")
    if up is not None:
        try:
            cfg = json.loads(up.read())
            t = cfg.get("tokens", {})
            st.session_state._utok = t.get("user_token", "")
            st.session_state._btok = t.get("bot_token", "")
            st.session_state.mapping = cfg.get("mapping", {})
            src_l = cfg.get("src_channels", [])
            tgt_l = cfg.get("tgt_channels", [])
            if src_l: st.session_state.src_channels = src_l
            if tgt_l: st.session_state.tgt_channels = tgt_l
            if st.session_state.mapping:
                storage.save_channel_mapping(st.session_state.mapping)
            s = cfg.get("settings", {})
            _log(f"✅ Config yüklendi — {len(st.session_state.mapping)} eşleme", "success")
            st.success(f"✅ {len(st.session_state.mapping)} eşleme yüklendi")
        except Exception as e:
            st.error(f"Config hatası: {e}")

    if st.button("🗑 DB Sıfırla", use_container_width=True):
        storage.reset_database()
        st.session_state.mapping = {}
        with _lock:
            _shared["stats"] = {"found":0,"sent":0,"active":"—"}
            _shared["progress"] = 0.0
        _log("🗑 DB sıfırlandı.", "warning")
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# SEKMELER
# ══════════════════════════════════════════════════════════════════════════════
tab1, tab2 = st.tabs(["📡 Kanal Eşleme", "📊 İlerleme & Log"])

with tab1:
    left, right = st.columns(2)

    with left:
        st.markdown("### 📥 Kaynak Kanallar")
        ug = st.session_state.user_guilds
        if ug:
            sg = st.selectbox("Kaynak Sunucu", ["— Seç —"]+[g["name"] for g in ug], key="sg_sel")
            if sg != "— Seç —":
                sgid = next((g["id"] for g in ug if g["name"] == sg), None)
                if sgid and st.button("🔄 Getir", key="btn_sg"):
                    with st.spinner("Yükleniyor..."):
                        chs = st.session_state.scraper.get_guild_channels(sgid)
                        st.session_state.src_channels = sorted(
                            [c for c in chs if c.get("type")==0],
                            key=lambda c: c.get("position",0))
                        _log(f"✅ {len(st.session_state.src_channels)} kaynak kanal", "success")
                        st.rerun()
        else:
            st.info("← Tokenları kontrol et")

        with st.expander("➕ Manuel Kanal ID"):
            msrc = st.text_input("Kanal ID", key="msrc")
            if st.button("Ekle", key="btn_msrc2") and msrc.strip():
                info = st.session_state.scraper.get_channel_info(msrc.strip()) if st.session_state.scraper else None
                if info and "id" in info:
                    if not any(c["id"]==info["id"] for c in st.session_state.src_channels):
                        st.session_state.src_channels.append(info)
                    _log(f"✅ #{info.get('name')} eklendi", "success")
                    st.rerun()
                else:
                    st.error("Bulunamadı.")

        if st.session_state.src_channels:
            sq = st.text_input("🔍 Ara", key="sq", placeholder="kanal adı...")
            shown = [c for c in st.session_state.src_channels
                     if sq.lower() in c["name"].lower()] if sq else st.session_state.src_channels

            ca, cb = st.columns(2)
            with ca:
                if st.button("✅ Tümünü Seç", use_container_width=True, key="bsa"):
                    for c in shown:
                        if c["id"] not in st.session_state.mapping:
                            st.session_state.mapping[c["id"]] = ""
                    st.rerun()
            with cb:
                if st.button("❌ Kaldır", use_container_width=True, key="bda"):
                    for c in shown:
                        st.session_state.mapping.pop(c["id"], None)
                    st.rerun()

            sel_count = sum(1 for c in shown if c["id"] in st.session_state.mapping)
            st.caption(f"{sel_count}/{len(shown)} seçili")

            snap = dict(st.session_state.mapping)
            changed = False
            for ch in shown:
                cid   = ch["id"]
                is_s  = cid in snap
                is_m  = bool(snap.get(cid))
                icon  = "🟢" if is_m else ("🔵" if is_s else "⚪")
                val   = st.checkbox(f"{icon} #{ch['name']}", value=is_s, key=f"c_{cid}")
                if val and cid not in st.session_state.mapping:
                    st.session_state.mapping[cid] = ""; changed = True
                elif not val and cid in st.session_state.mapping:
                    del st.session_state.mapping[cid]; changed = True
            if changed:
                st.rerun()

    with right:
        st.markdown("### 📤 Hedef & Eşleme")
        bg = st.session_state.bot_guilds
        if bg:
            tg = st.selectbox("Hedef Sunucu (Bot)", ["— Seç —"]+[g["name"] for g in bg], key="tg_sel")
            if tg != "— Seç —":
                tgid = next((g["id"] for g in bg if g["name"] == tg), None)
                if tgid and st.button("🔄 Getir", key="btn_tg"):
                    with st.spinner("Yükleniyor..."):
                        chs = st.session_state.sender.get_guild_channels(tgid)
                        st.session_state.tgt_channels = sorted(
                            [c for c in chs if c.get("type")==0],
                            key=lambda c: c.get("position",0))
                        _log(f"✅ {len(st.session_state.tgt_channels)} hedef kanal", "success")
                        st.rerun()
        else:
            st.info("← Tokenları kontrol et")

        with st.expander("➕ Manuel Sunucu ID"):
            mtgt = st.text_input("Sunucu ID", key="mtgt")
            if st.button("Ekle", key="btn_mtgt2") and mtgt.strip():
                chs = st.session_state.sender.get_guild_channels(mtgt.strip()) if st.session_state.sender else []
                if chs:
                    st.session_state.tgt_channels = sorted(
                        [c for c in chs if c.get("type")==0],
                        key=lambda c: c.get("position",0))
                    _log(f"✅ {len(st.session_state.tgt_channels)} hedef kanal", "success")
                    st.rerun()
                else:
                    st.error("Bulunamadı / bot davet edilmedi.")

        if st.session_state.tgt_channels and st.session_state.mapping:
            tq = st.text_input("🔍 Hedef Ara", key="tq")
            tmap = {f"#{c['name']}": c["id"] for c in st.session_state.tgt_channels}
            tlist = [n for n in tmap if tq.lower() in n.lower()] if tq else list(tmap)

            mapped = sum(1 for v in st.session_state.mapping.values() if v)
            total  = len(st.session_state.mapping)
            st.progress(mapped/max(total,1), text=f"{mapped}/{total} eşlendi")
            st.caption("Kaynak → Hedef:")

            for src_id in list(st.session_state.mapping.keys()):
                src_nm = next((c["name"] for c in st.session_state.src_channels
                               if c["id"]==src_id), src_id[:10]+"…")
                cur_tid  = st.session_state.mapping.get(src_id, "")
                cur_tnm  = next((f"#{c['name']}" for c in st.session_state.tgt_channels
                                 if c["id"]==cur_tid), "— Seç —")
                opts = ["— Seç —"] + tlist
                idx  = opts.index(cur_tnm) if cur_tnm in opts else 0
                chosen = st.selectbox(f"#{src_nm}", opts, index=idx, key=f"ts_{src_id}")
                new_tid = tmap.get(chosen, "")
                if new_tid != st.session_state.mapping.get(src_id, ""):
                    st.session_state.mapping[src_id] = new_tid
        elif not st.session_state.tgt_channels:
            st.info("← Hedef sunucuyu yükle")
        else:
            st.info("← Kaynak kanal seç")

with tab2:
    with _lock:
        stats    = dict(_shared["stats"])
        progress = _shared["progress"]
        running  = _shared["running"]
        logs     = list(_shared["logs"])

    c1,c2,c3,c4 = st.columns(4)
    with c1: st.markdown(f'<div class="stat-card"><div class="stat-val">{stats["found"]}</div><div class="stat-lbl">Bulunan</div></div>', unsafe_allow_html=True)
    with c2: st.markdown(f'<div class="stat-card"><div class="stat-val">{stats["sent"]}</div><div class="stat-lbl">Gönderilen</div></div>', unsafe_allow_html=True)
    with c3: st.markdown(f'<div class="stat-card"><div class="stat-val">{storage.get_sent_count()}</div><div class="stat-lbl">Toplam DB</div></div>', unsafe_allow_html=True)
    with c4:
        d = "🟢 Çalışıyor" if running else "⚫ Bekliyor"
        st.markdown(f'<div class="stat-card"><div class="stat-val" style="font-size:15px">{d}</div><div class="stat-lbl">Durum</div></div>', unsafe_allow_html=True)

    st.caption(f"Aktif: {stats['active']}")
    st.progress(progress, text=f"{progress*100:.1f}%")
    st.divider()

   
