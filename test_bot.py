"""Offline checks. Run: docker compose run --rm bot python test_bot.py"""
import asyncio
import json

import bot


def embed(entity):
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


# Spotify link forms
for link, expected in [
    ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc", ("track", "4cOdK2wGLETKBW3PvgPWqT")),
    ("https://open.spotify.com/intl-de/album/4aawyAB9vmqN3uQ7FjRGTy", ("album", "4aawyAB9vmqN3uQ7FjRGTy")),
    ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ("https://open.spotify.com/user/someone/playlist/37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
]:
    assert bot.SPOTIFY_RE.search(link).groups() == expected, link
assert bot.SPOTIFY_RE.search("https://open.spotify.com/artist/0gxyHStUsqpMadRV0Di1Qt") is None

# Spotify embed parsing -> YouTube searches
tracks = bot.parse_spotify_embed("track", embed({"name": "Never Gonna Give You Up", "artists": [{"name": "Rick Astley"}]}))
assert tracks == [bot.Track("Rick Astley - Never Gonna Give You Up", "ytsearch1:Rick Astley - Never Gonna Give You Up audio")]
tracks = bot.parse_spotify_embed("album", embed({"trackList": [{"title": "Song", "subtitle": "A,\xa0B"}, {"title": "Solo"}]}))
assert [t.title for t in tracks] == ["A, B - Song", "Solo"]
assert tracks[1].url == "ytsearch1:Solo audio"
try:
    bot.parse_spotify_embed("track", "<html></html>")
    raise AssertionError("expected ValueError")
except ValueError:
    pass

# Non-YouTube/Spotify URLs are rejected before reaching yt-dlp
try:
    asyncio.run(bot.resolve("https://example.com/video.mp4"))
    raise AssertionError("expected ValueError")
except ValueError:
    pass

print("ok")
