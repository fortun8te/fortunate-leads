"""Map raw Instagram payloads onto Apify's Instagram Profile Scraper dataset schema.

Field names and their ordering follow the sample published on
https://apify.com/apify/instagram-profile-scraper (see docs/apify-parity.md).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

IG = "https://www.instagram.com"
_HASHTAG_RE = re.compile(r"#([^\s#]+)")
_MENTION_RE = re.compile(r"@([^\s@]+)")
_SC_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


# Key order of Apify's published NASA sample, then the extra keys from the actor's dataset schema.
APIFY_ORDER = ["inputUrl", "id", "username", "url", "fullName", "biography", "about", "followersCount",
               "followsCount", "postsCount", "highlightReelCount", "igtvVideoCount", "isBusinessAccount",
               "joinedRecently", "hasChannel", "businessCategoryName", "private", "verified", "externalUrl",
               "externalUrlShimmed", "externalUrls", "profilePicUrl", "profilePicUrlHD", "relatedProfiles",
               "latestPosts", "latestIgtvVideos", "fbid", "facebookPage", "businessAddress", "hasPublicStory",
               "isRestrictedProfile", "restrictionReason", "threadsNetProfile"]


TOP_ORDER = ["inputUrl", "id", "username", "url", "fullName", "biography", "about", "externalUrls", "externalUrl",
             "externalUrlShimmed", "followersCount", "followsCount", "hasChannel", "highlightReelCount",
             "isBusinessAccount", "joinedRecently", "businessCategoryName", "private", "verified", "profilePicUrl",
             "profilePicUrlHD", "igtvVideoCount", "relatedProfiles", "latestIgtvVideos", "postsCount", "latestPosts",
             "businessAddress", "fbid"]


def order_item(item: dict) -> dict:
    out = {k: item[k] for k in TOP_ORDER if k in item}
    out.update({k: v for k, v in item.items() if k not in out})
    return out


def with_coverage(item: dict, source: str, *, bio_observed: bool = True) -> dict:
    """Keep missing data distinct from an observed empty bio, including in CSV exports."""
    bio = item.get("biography")
    valid = bio_observed and isinstance(bio, str)
    item["_scrape"] = {"source": source, "observedAt": datetime.now(timezone.utc).isoformat(),
                       "bioStatus": ("observed" if bio else "empty") if valid else "unavailable",
                       "cacheHit": False}
    if not valid:
        item["error"] = "incomplete_profile"
        item["errorDescription"] = "Profile partially read; Instagram biography was unavailable"
        item["requestErrorMessages"] = [item["errorDescription"]]
    return item


def iso(ts: int | float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def shortcode_from_pk(pk: str | int) -> str:
    n = int(str(pk).split("_")[0])
    out = ""
    while n > 0:
        n, r = divmod(n, 64)
        out = _SC_ALPHABET[r] + out
    return out


def _uniq(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


def hashtags(text: str | None) -> list[str]:
    return _uniq(re.findall(r"#(\S+?)(?=\s|#|$)", text or ""))


def mentions(text: str | None) -> list[str]:
    return _uniq([m.rstrip(".") for m in re.findall(r"(?<!\w)@([A-Za-z0-9_.]+)", text or "") if m.rstrip(".")])


def _count(obj: Any) -> int | None:
    if isinstance(obj, dict):
        return obj.get("count")
    return None


def _edges(obj: Any) -> list[dict]:
    if not isinstance(obj, dict):
        return []
    return [e.get("node", {}) for e in obj.get("edges") or [] if isinstance(e, dict)]


def _mini_user(u: dict) -> dict:
    return {
        "full_name": u.get("full_name"),
        "id": str(u.get("id") or u.get("pk") or "") or None,
        "is_verified": u.get("is_verified"),
        "profile_pic_url": u.get("profile_pic_url"),
        "username": u.get("username"),
    }


# --------------------------------------------------------------------------- GraphQL-shaped
_GRAPH_TYPE = {"GraphImage": "Image", "GraphVideo": "Video", "GraphSidecar": "Sidecar",
               "XDTGraphImage": "Image", "XDTGraphVideo": "Video", "XDTGraphSidecar": "Sidecar"}


def map_graph_post(n: dict, owner_username: str | None, owner_id: str | None, child: bool = False) -> dict:
    caption_edges = _edges(n.get("edge_media_to_caption"))
    caption = caption_edges[0].get("text", "") if caption_edges else ""
    typ = _GRAPH_TYPE.get(n.get("__typename"), "Video" if n.get("is_video") else "Image")
    dims = n.get("dimensions") or {}
    children = _edges(n.get("edge_sidecar_to_children"))
    owner = n.get("owner") or {}
    o_user = owner.get("username") or owner_username
    o_id = str(owner.get("id") or owner_id or "") or None
    post: dict[str, Any] = {
        "id": str(n.get("id")),
        "type": typ,
        "shortCode": n.get("shortcode"),
        "caption": caption,
        "hashtags": hashtags(caption),
        "mentions": mentions(caption),
        "url": f"{IG}/p/{n.get('shortcode')}/",
        "commentsCount": _count(n.get("edge_media_to_comment")) or 0,
    }
    if child:
        post["firstComment"] = ""
        post["latestComments"] = []
    post.update({
        "dimensionsHeight": dims.get("height"),
        "dimensionsWidth": dims.get("width"),
        "displayUrl": n.get("display_url"),
        "images": [c.get("display_url") for c in children] if (typ == "Sidecar" and not child) else [],
    })
    if n.get("is_video") and n.get("video_url"):
        post["videoUrl"] = n.get("video_url")
    post["alt"] = n.get("accessibility_caption")
    likes = _count(n.get("edge_liked_by")) if not child else None
    if likes is None and not child:
        likes = _count(n.get("edge_media_preview_like"))
    post["likesCount"] = likes
    if n.get("is_video"):
        post["videoViewCount"] = n.get("video_view_count")
    post["timestamp"] = iso(n.get("taken_at_timestamp")) if not child else None
    post["childPosts"] = [map_graph_post(c, o_user, o_id, child=True) for c in children] if not child else []
    loc = n.get("location") or {}
    if loc and not child:
        post["locationName"] = loc.get("name")
        post["locationId"] = str(loc.get("id"))
    post["ownerUsername"] = o_user
    post["ownerId"] = o_id
    if child:
        tagged = [_mini_user((t.get("user") or {})) for t in _edges(n.get("edge_media_to_tagged_user"))]
        if tagged:
            post["taggedUsers"] = tagged
    if not child:
        if n.get("is_video") and n.get("product_type"):
            post["productType"] = n.get("product_type")
        tagged = [_mini_user((t.get("user") or {})) for t in _edges(n.get("edge_media_to_tagged_user"))]
        if tagged:
            post["taggedUsers"] = tagged
        pinned = n.get("pinned_for_users") or []
        if any(str(p.get("id")) == str(o_id) for p in pinned):
            post["isPinned"] = True
        mi = n.get("clips_music_attribution_info")
        if isinstance(mi, dict) and mi.get("uses_original_audio") is False:
            post["musicInfo"] = {k: mi.get(k) for k in ("artist_name", "song_name", "uses_original_audio",
                                                        "should_mute_audio", "should_mute_audio_reason", "audio_id")}
        post["isCommentsDisabled"] = bool(n.get("comments_disabled"))
    return post


def map_graph_igtv(n: dict, owner_username: str | None, owner_id: str | None) -> dict:
    caption_edges = _edges(n.get("edge_media_to_caption"))
    caption = caption_edges[0].get("text", "") if caption_edges else ""
    dims = n.get("dimensions") or {}
    owner = n.get("owner") or {}
    o_id = str(owner.get("id") or owner_id or "") or None
    out = {
        "type": "Video",
        "shortCode": n.get("shortcode"),
        "title": n.get("title"),
        "caption": caption,
        "commentsCount": _count(n.get("edge_media_to_comment")) or 0,
        "commentsDisabled": bool(n.get("comments_disabled")),
        "dimensionsHeight": dims.get("height"),
        "dimensionsWidth": dims.get("width"),
        "displayUrl": n.get("display_url"),
        "likesCount": _count(n.get("edge_liked_by")) or _count(n.get("edge_media_preview_like")),
        "videoDuration": n.get("video_duration"),
        "videoViewCount": n.get("video_view_count"),
        "id": str(n.get("id")),
        "hashtags": hashtags(caption),
        "mentions": mentions(caption),
        "url": f"{IG}/p/{n.get('shortcode')}/",
        "firstComment": "",
        "latestComments": [],
        "images": [],
        "videoUrl": n.get("video_url"),
        "alt": n.get("accessibility_caption"),
        "timestamp": iso(n.get("taken_at_timestamp")),
        "childPosts": [],
        **({"locationName": (n.get("location") or {}).get("name"), "locationId": str((n.get("location") or {}).get("id"))}
           if n.get("location") else {}),
        "ownerUsername": owner.get("username") or owner_username,
        "ownerId": o_id,
        "productType": n.get("product_type"),
    }
    tagged = [_mini_user((t.get("user") or {})) for t in _edges(n.get("edge_media_to_tagged_user"))]
    if tagged:
        out["taggedUsers"] = tagged
    out["isCommentsDisabled"] = bool(n.get("comments_disabled"))
    return out


def map_web_profile_info(user: dict, input_url: str, include_about: bool = False) -> dict:
    uname = user.get("username")
    uid = instagram_id(user)
    item: dict[str, Any] = {
        "inputUrl": input_url,
        "id": uid,
        "username": uname,
        "url": f"{IG}/{uname}",
        "fullName": user.get("full_name"),
        "biography": user.get("biography"),
    }
    if include_about:
        item["about"] = None  # placement unknown; requires a logged-in "About this account" call; see docs/apify-parity.md
    item.update({
        "externalUrls": [
            {"title": b.get("title"), "lynx_url": b.get("lynx_url"), "url": b.get("url"), "link_type": b.get("link_type")}
            for b in (user.get("bio_links") or [])
        ],
        **({"externalUrl": user.get("external_url"), "externalUrlShimmed": user.get("external_url_linkshimmed")}
           if user.get("external_url") else {}),
        "followersCount": _count(user.get("edge_followed_by")),
        "followsCount": _count(user.get("edge_follow")),
        "hasChannel": user.get("has_channel"),
        "highlightReelCount": user.get("highlight_reel_count"),
        "isBusinessAccount": user.get("is_business_account"),
        "joinedRecently": user.get("is_joined_recently"),
        "businessCategoryName": user.get("business_category_name") or user.get("category_name"),
        "private": user.get("is_private"),
        "verified": user.get("is_verified"),
        "profilePicUrl": user.get("profile_pic_url"),
        "profilePicUrlHD": user.get("profile_pic_url_hd"),
        "igtvVideoCount": _count(user.get("edge_felix_video_timeline")),
        "relatedProfiles": [
            {"id": str(r.get("id")), "full_name": r.get("full_name"), "is_private": r.get("is_private"),
             "is_verified": r.get("is_verified"), "profile_pic_url": r.get("profile_pic_url"), "username": r.get("username")}
            for r in _edges(user.get("edge_related_profiles"))
        ],
        "latestIgtvVideos": [map_graph_igtv(n, uname, uid) for n in _edges(user.get("edge_felix_video_timeline"))],
        "postsCount": _count(user.get("edge_owner_to_timeline_media")),
        "latestPosts": [map_graph_post(n, uname, uid) for n in _edges(user.get("edge_owner_to_timeline_media"))],
        "fbid": user.get("fbid"),
    })
    addr = _business_address(user.get("business_address_json"))
    if addr:
        item["businessAddress"] = addr
    return order_item(item)


def _business_address(raw: Any) -> dict | None:
    import json
    if not raw:
        return None
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(d, dict) or not any(v for v in d.values()):
        return None
    return d


def _extra_fields(user: dict) -> dict:
    """Fields listed in the actor's published dataset schema beyond the README sample."""
    uname = user.get("username")
    return {
        "businessAddress": _business_address(user.get("business_address_json")),
        "facebookPage": None,          # not exposed on logged-out surfaces
        "hasPublicStory": None,        # not exposed by web_profile_info
        "isRestrictedProfile": None,
        "restrictionReason": None,
        "threadsNetProfile": {"username": uname} if user.get("has_onboarded_to_text_post_app") else None,
    }


# --------------------------------------------------------------------------- v1 (mobile REST) media
def _best(cands: list[dict] | None) -> str | None:
    if not cands:
        return None
    return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0)).get("url")


def map_v1_media(m: dict, child: bool = False, owner: dict | None = None) -> dict:
    mt = m.get("media_type")
    typ = {1: "Image", 2: "Video", 8: "Sidecar"}.get(mt, "Image")
    owner = owner or m.get("user") or {}
    o_user, o_id = owner.get("username"), str(owner.get("pk") or owner.get("id") or "") or None
    cap = (m.get("caption") or {}).get("text", "") if not child else ""
    code = m.get("code") or shortcode_from_pk(m.get("pk") or m.get("id"))
    children = m.get("carousel_media") or []
    display = _best((m.get("image_versions2") or {}).get("candidates"))
    post: dict[str, Any] = {
        "id": str(m.get("pk") or str(m.get("id", "")).split("_")[0]),
        "type": typ,
        "shortCode": code,
        "caption": cap,
        "hashtags": hashtags(cap),
        "mentions": mentions(cap),
        "url": f"{IG}/p/{code}/",
        "commentsCount": (m.get("comment_count") or 0) if not child else 0,
    }
    if child:
        post["firstComment"] = ""
        post["latestComments"] = []
    post.update({
        "dimensionsHeight": m.get("original_height"),
        "dimensionsWidth": m.get("original_width"),
        "displayUrl": display,
        "images": [_best((c.get("image_versions2") or {}).get("candidates")) for c in children] if (typ == "Sidecar" and not child) else [],
    })
    if typ == "Video" and m.get("video_versions"):
        post["videoUrl"] = m["video_versions"][0].get("url")
    post["alt"] = m.get("accessibility_caption")
    post["likesCount"] = m.get("like_count") if not child else None
    if typ == "Video" and not child:
        post["videoViewCount"] = m.get("view_count") or m.get("play_count") or m.get("ig_play_count")
    post["timestamp"] = iso(m.get("taken_at")) if not child else None
    post["childPosts"] = [map_v1_media(c, child=True, owner=owner) for c in children] if not child else []
    post["ownerUsername"] = o_user
    post["ownerId"] = o_id
    if not child:
        if typ == "Video" and m.get("product_type"):
            post["productType"] = m.get("product_type")
        tags = [_mini_user(t.get("user") or {}) for t in ((m.get("usertags") or {}).get("in") or [])]
        if tags:
            post["taggedUsers"] = tags
        if o_id and str(o_id) in [str(x) for x in (m.get("timeline_pinned_user_ids") or [])]:
            post["isPinned"] = True
        post["isCommentsDisabled"] = bool(m.get("comments_disabled") or m.get("disable_caption_and_comment"))
    return post


# --------------------------------------------------------------------------- HTML (logged-out SSR)


def _find_user_dict(o: Any, username: str | None = None) -> dict | None:
    """Choose a profile object, ignoring recommendations and incomplete duplicates."""
    candidates = []
    pending = [o]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            name = value.get("username")
            if (isinstance(name, str) and
                    ("follower_count" in value or "biography" in value) and
                    (username is None or name.casefold() == username.casefold())):
                candidates.append(value)
            pending.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            pending.extend(reversed(value))
    return max(candidates, key=_profile_completeness) if candidates else None


def _profile_completeness(user: dict) -> tuple[bool, bool, int]:
    # Never prefer a truncated metadata summary over the actual biography field.
    return (isinstance(user.get("biography"), str), instagram_id(user, html=True) is not None,
            sum(value is not None for value in user.values()))


def parse_profile_html(page: str, username: str | None = None) -> dict:
    """Pull the requested logged-out SSR profile object and its OG description."""
    import json
    from html.parser import HTMLParser

    class ProfileHTMLParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.user: dict = {}
            self.og_description: str | None = None
            self.json_chunks: list[str] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "meta" and values.get("property") == "og:description":
                self.og_description = values.get("content")
            elif tag == "script" and (values.get("type") or "").lower() == "application/json":
                self.json_chunks = []

        def handle_data(self, data: str) -> None:
            if self.json_chunks is not None:
                self.json_chunks.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag != "script" or self.json_chunks is None:
                return
            blob = "".join(self.json_chunks)
            self.json_chunks = None
            try:
                found = _find_user_dict(json.loads(blob), username)
            except (ValueError, RecursionError):
                return
            if found and (not self.user or _profile_completeness(found) > _profile_completeness(self.user)):
                self.user = dict(found)

    parser = ProfileHTMLParser()
    parser.feed(page)
    parser.close()
    out = parser.user
    if parser.og_description is not None:
        out["_og_description"] = parser.og_description
    return out


def _parse_exact_int(s: str) -> int | None:
    s = s.replace(",", "")
    return int(s) if s.isdigit() else None


def og_counts(desc: str | None) -> dict:
    """'4,931 Posts' is exact; '32K Posts' is rounded and therefore NOT used."""
    res: dict[str, int | None] = {}
    if not desc:
        return res
    m = re.search(r"([\d.,KMB]+) Posts", desc)
    if m:
        res["posts"] = _parse_exact_int(m.group(1))
    return res


def instagram_id(user: dict | None, *, html: bool = False) -> str | None:
    """SSR ``id`` is an FB id; only SSR ``pk`` identifies the Instagram account."""
    user = user or {}
    value = user.get("pk") if html else user.get("pk") or user.get("id")
    return str(value) if value is not None and value != "" else None


def same_profile(user: dict | None, user_id: str | None, username: str | None, *, html: bool = False) -> bool:
    """Reject a disagreeing identity while allowing fields omitted by a source."""
    if not user:
        return False
    found_id = instagram_id(user, html=html)
    found_name = user.get("username")
    if html and user_id and not found_id:
        return False  # same handle cannot establish that an SSR bio belongs to the feed's id
    return not ((user_id and found_id and user_id != found_id) or
                (username and found_name and str(username).casefold() != str(found_name).casefold()))


def map_fallback(input_url: str, feed: dict | None, page_user: dict | None, include_about: bool = False,
                 threads_user: dict | None = None, gql_user: dict | None = None) -> dict:
    """Assemble an item from feed/user/<name>/username (posts + basic user) and the SSR HTML profile.

    Used when web_profile_info fails for an account (e.g. Instagram's current
    "ig_business_category_subvertical has been deleted" 400 for some business profiles).
    Fields Instagram does not expose on these surfaces are left as null.
    """
    feed = feed or {}
    page_user = page_user or {}
    fu = feed.get("user") or {}
    items = feed.get("items") or []
    if not fu and items:
        fu = items[0].get("user") or {}
    # Feed and GraphQL carry Instagram ids. HTML's id is an FB id, so only its pk is comparable.
    canonical_id = (instagram_id(fu) or instagram_id(gql_user) or
                    instagram_id(threads_user) or instagram_id(page_user, html=True))
    canonical_name = (fu.get("username") or (gql_user or {}).get("username") or
                      (threads_user or {}).get("username") or page_user.get("username"))
    if not same_profile(page_user, canonical_id, canonical_name, html=True):
        page_user = {}
    if not same_profile(threads_user, canonical_id, canonical_name):
        threads_user = None
    if not same_profile(gql_user, canonical_id, canonical_name):
        gql_user = None
    uname = fu.get("username") or page_user.get("username")
    uid = instagram_id(fu) or instagram_id(page_user, html=True)
    item: dict[str, Any] = {
        "inputUrl": input_url,
        "id": uid,
        "username": uname,
        "url": f"{IG}/{uname}",
        "fullName": fu.get("full_name") or page_user.get("full_name"),
        "biography": page_user.get("biography"),
    }
    if include_about:
        item["about"] = None
    item.update({
        "externalUrls": [],
        "externalUrl": None,
        "externalUrlShimmed": None,
        "followersCount": page_user.get("follower_count"),
        "followsCount": page_user.get("following_count"),
        "hasChannel": None,
        "highlightReelCount": None,
        "isBusinessAccount": None,
        "joinedRecently": None,
        "businessCategoryName": None,
        "private": fu.get("is_private", page_user.get("is_private")),
        "verified": fu.get("is_verified", page_user.get("is_verified")),
        "profilePicUrl": page_user.get("profile_pic_url") or fu.get("profile_pic_url"),
        "profilePicUrlHD": None,
        "igtvVideoCount": None,
        "relatedProfiles": [],
        "latestIgtvVideos": [],
        "postsCount": og_counts(page_user.get("_og_description")).get("posts"),
        "latestPosts": [map_v1_media(m, owner=fu) for m in items[:12]],
        # On the SSR surface "id" is the FB-graph id (1784...), "pk" is the Instagram id.
        "fbid": page_user.get("id") if page_user.get("id") and str(page_user.get("id")) != uid else None,
    })
    item.update({"businessAddress": None, "facebookPage": None, "hasPublicStory": None,
                 "isRestrictedProfile": None, "restrictionReason": None, "threadsNetProfile": None})
    t = threads_user or {}
    if t:
        # Only take fields that are identical across IG/Threads surfaces (verified on nasa/natgeo 2026-09-24).
        # NOT taken: edge_follow (Threads following count), business_category_name (null there), posts.
        links = [{"title": b.get("title"), "lynx_url": None, "url": b.get("url"), "link_type": b.get("link_type")}
                 for b in (t.get("bio_links") or [])]
        uname = uname or t.get("username")
        item.update({
            "id": item["id"] or instagram_id(t),
            "username": uname, "url": f"{IG}/{uname}",
            "fullName": item["fullName"] or t.get("full_name"),
            "biography": item["biography"] if item["biography"] is not None else t.get("biography"),
            "externalUrls": links,
            "externalUrl": links[0]["url"] if links else None,
            "followersCount": item["followersCount"] if item["followersCount"] is not None else _count(t.get("edge_followed_by")),
            "hasChannel": t.get("has_channel"),
            "highlightReelCount": t.get("highlight_reel_count"),
            "isBusinessAccount": t.get("is_business_account"),
            "joinedRecently": t.get("is_joined_recently"),
            "private": item["private"] if item["private"] is not None else t.get("is_private"),
            "verified": item["verified"] if item["verified"] is not None else t.get("is_verified"),
            "profilePicUrlHD": t.get("profile_pic_url_hd"),
            "igtvVideoCount": _count(t.get("edge_felix_video_timeline")),
            "relatedProfiles": [
                {"id": str(r.get("id")), "full_name": r.get("full_name"), "is_private": r.get("is_private"),
                 "is_verified": r.get("is_verified"), "profile_pic_url": r.get("profile_pic_url"), "username": r.get("username")}
                for r in _edges(t.get("edge_related_profiles"))],
            "latestIgtvVideos": [map_graph_igtv(n, uname, item["id"]) for n in _edges(t.get("edge_felix_video_timeline"))],
            "fbid": item["fbid"] or t.get("fbid"),
            "threadsNetProfile": {"username": uname} if t.get("has_onboarded_to_text_post_app") else None,
        })
    g = gql_user or {}
    if g:
        # PolarisProfilePageContentQuery (logged-out): real Instagram values, preferred over Threads/HTML ones.
        links = [{"title": b.get("title"), "lynx_url": b.get("lynx_url"), "url": b.get("url"),
                  "link_type": b.get("link_type")} for b in (g.get("bio_links") or [])]
        uname = item["username"] or g.get("username")
        hd = (g.get("hd_profile_pic_url_info") or {}).get("url")
        item.update({
            "id": item["id"] or instagram_id(g),
            "username": uname, "url": f"{IG}/{uname}",
            "fullName": g["full_name"] if "full_name" in g else item["fullName"],
            "biography": g["biography"] if isinstance(g.get("biography"), str) else item["biography"],
            "externalUrls": links if "bio_links" in g else item["externalUrls"],
            "externalUrl": (g.get("external_url") if "external_url" in g else
                            (links[0]["url"] if links else None) if "bio_links" in g else item["externalUrl"]),
            "externalUrlShimmed": (g.get("external_lynx_url") if "external_lynx_url" in g else
                                   None if "bio_links" in g else item["externalUrlShimmed"]),
            "followersCount": g["follower_count"] if g.get("follower_count") is not None else item["followersCount"],
            "followsCount": g["following_count"] if g.get("following_count") is not None else item["followsCount"],
            "private": g["is_private"] if g.get("is_private") is not None else item["private"],
            "verified": g["is_verified"] if g.get("is_verified") is not None else item["verified"],
            "profilePicUrl": g["profile_pic_url"] if "profile_pic_url" in g else item["profilePicUrl"],
            "profilePicUrlHD": hd or item["profilePicUrlHD"],
            "businessCategoryName": g.get("category") or item["businessCategoryName"],   # "" logged-out -> keep null
            "postsCount": g["media_count"] if g.get("media_count") is not None else item["postsCount"],
            "fbid": item["fbid"] or g.get("fbid_v2"),
        })
    return order_item(item)


from .about import map_about  # noqa: E402,F401  (About-section mapper, used when a session is configured)
