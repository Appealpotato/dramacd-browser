"""Chil-Chil (chil-chil.net) metadata source — the BL drama CD database.

Goods URLs:  https://www.chil-chil.net/goodsDetail/goods_id/{numeric}/
Search:      POST /goodsList/ with action_goodsList=search_form&freeword=...
             (the ?word= GET param is silently ignored — must POST)
Covers:      https://img.chil-chil.net/goods_img/L/{goods_id:08d}_L.jpg

CD pages carry per-story cast blocks (表題作 / 同時収録作) with seme/uke
roles plus an "other characters" line, and a 作品情報 dl-table with maker,
series, runtime, disc count, JAN and package release date."""
import re
import unicodedata
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.chil-chil.net"

# "キャラ名[声優名]" pairs in the その他キャラ line
_OTHER_CAST_RE = re.compile(r"([^／\[\]]+?)\[([^\]]+)\]")


class ChilChilSource(MetadataSource):
    name = "chil_chil"
    label = "Chil-Chil"
    url_example = "https://www.chil-chil.net/goodsDetail/goods_id/23936/"
    supports_search = True
    _url_re = re.compile(r"chil-chil\.net/goodsDetail/goods_id/(\d+)", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a goods URL: {url}")
        goods_id = int(m.group(1))
        canonical = f"{BASE}/goodsDetail/goods_id/{goods_id}/"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical, goods_id)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        html = await self._post(
            client,
            f"{BASE}/goodsList/",
            {"action_goodsList": "search_form", "freeword": query},
        )
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str, goods_id: int) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        h1 = soup.select_one(".c-detail_title h1.title")
        if h1:
            meta["title"] = h1.get_text(strip=True)
        lang = soup.select_one(".c-detail_title p.lang")
        if lang and lang.get_text(strip=True):
            meta["extra"]["romaji"] = lang.get_text(strip=True)

        # Cover: any goods_img on the page (skip the noimage placeholder),
        # else derive from the predictable CDN pattern.
        cover = None
        for img in soup.select('img[src*="goods_img"]'):
            src = img.get("src") or ""
            if "noimage" in src:
                continue
            if f"{goods_id:08d}" in src:
                cover = src
                break
        meta["cover_url"] = cover or f"https://img.chil-chil.net/goods_img/L/{goods_id:08d}_L.jpg"

        # Cast: per-story blocks. Collect a flat ordered de-duped seiyuu list
        # plus a structured per-story breakdown for notes.
        cast_detail = []
        for story in soup.select("section.c-story"):
            title_el = story.select_one("h2.c-title01")
            sub = title_el.select_one("span.sub") if title_el else None
            story_entry = {
                "story": sub.get_text(strip=True) if sub else None,
                "cast": [],
            }
            for role_cls, role in (("c-story_seme", "攻"), ("c-story_uke", "受")):
                for p in story.select(f".{role_cls} .chara"):
                    actor_a = p.select_one('a[href*="voiceDetail"]')
                    if not actor_a:
                        continue
                    actor = actor_a.get_text(strip=True)
                    chara = p.get_text(strip=True).split("→")[0].strip()
                    story_entry["cast"].append({"role": role, "character": chara, "actor": actor})
                    self._add_seiyuu(meta, actor)
            other = story.select_one("dl.c-story_other dd")
            if other:
                # Links give the canonical actor names; the regex pairs them
                # with character names from the surrounding text.
                text = other.get_text(" ", strip=True)
                for chara, actor in _OTHER_CAST_RE.findall(text):
                    story_entry["cast"].append(
                        {"role": "other", "character": chara.strip(), "actor": actor.strip()}
                    )
                for a in other.select('a[href*="voiceDetail"]'):
                    self._add_seiyuu(meta, a.get_text(strip=True))
            if story_entry["cast"]:
                cast_detail.append(story_entry)
        if cast_detail:
            meta["extra"]["cast_detail"] = cast_detail

        synopsis = soup.select_one(".c-synopsis .c-synopsis_text")
        if synopsis:
            meta["description"] = synopsis.get_text("\n", strip=True) or None

        # 作品情報 dl table
        info: dict[str, str] = {}
        for dl in soup.select(".c-work_info .c-basicdata01 dl"):
            dt, dd = dl.select_one("dt"), dl.select_one("dd")
            if not dt or not dd:
                continue
            key = dt.get_text(strip=True)
            time_el = dd.select_one("time[datetime]")
            val = time_el["datetime"] if time_el else dd.get_text(" ", strip=True).replace("\xa0", " ")
            if key and val:
                info[key] = val

        meta["release_date"] = normalize_date(
            info.get("パッケージ発売日") or info.get("発売日") or info.get("配信開始日")
        )
        meta["jan"] = info.get("JANコード")
        meta["maker"] = info.get("メーカー") or info.get("出版社")
        meta["series"] = info.get("シリーズ")
        # Everything else from the table is still gold (脚本/収録時間/枚数...)
        consumed = {"パッケージ発売日", "発売日", "配信開始日", "JANコード",
                    "メーカー", "出版社", "シリーズ", "作品名"}
        leftovers = {k: v for k, v in info.items() if k not in consumed}
        if leftovers:
            meta["extra"]["work_info"] = leftovers

        return meta

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for block in soup.select("div.c-list"):
            title_a = block.select_one("h2.c-list_title a[href*='goodsDetail']")
            if not title_a:
                continue
            hit = {
                "source": self.name,
                "title": title_a.get_text(strip=True),
                "url": urljoin(BASE, title_a["href"]),
                "thumbnail": None,
                "release_date": None,
                "price": None,
                "category": None,
            }
            icn = block.select_one(".c-list_icn")
            if icn:
                # Category chips use full-width latin (ＣＤ) — normalize.
                hit["category"] = unicodedata.normalize("NFKC", icn.get_text(strip=True))
            img = block.select_one(".c-list_cover img[src]")
            if img and "noimage" not in (img.get("src") or ""):
                hit["thumbnail"] = img["src"]
            time_el = block.select_one("time[datetime]")
            if time_el:
                hit["release_date"] = normalize_date(time_el["datetime"])
            hits.append(hit)
        return hits

    @staticmethod
    def _add_seiyuu(meta: dict, name: str):
        name = (name or "").strip()
        if name and name not in meta["seiyuu"]:
            meta["seiyuu"].append(name)
