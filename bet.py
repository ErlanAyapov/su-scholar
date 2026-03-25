import requests
import json
import re


def cse_search(query: str):
    url = "https://cse.google.com/cse/element/v1"

    params = {
        "rsz": "filtered_cse",
        "num": "10",
        "hl": "ru",
        "source": "gcsc",
        "cselibv": "b33cba5881f68fbf",
        "cx": "010629848870696206954:x_duvrvu9da",
        "q": query,
        "safe": "off",
        "cse_tok": "AEXjvhLcvpcpaKsRt_NGPrsZMPit:1774414517502",  # быстро тухнет
        "sort": "",
        "exp": "cc",
        "fexp": "121574859,121574858,73152292,73152290",
        "oq": query,
        "callback": "google.search.cse.api10614",
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Referer": "https://official.satbayev.university/ru/teachers/abdullaeva-asel-seydullaevn",
        "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
    }

    session = requests.Session()
    resp = session.get(url, params=params, headers=headers, timeout=20)
    print(resp.status_code)
    print(resp.text[:500])

    resp.raise_for_status()

    m = re.search(r'^[^(]+\((.*)\)\s*$', resp.text, re.S)
    if not m:
        raise ValueError("Не удалось распарсить JSONP")

    return json.loads(m.group(1))


data = cse_search("Албанбай")
print(data.keys())