"""IDX security master: normalized issuer-name keys -> (IDX ticker, short name, sector).

ISINs are taken from the N-PORT filings themselves (authoritative); this table only
maps issuer names, which both N-PORT ("Bank Central Asia Tbk PT") and NBIM
("Bank Central Asia Tbk PT") report, onto IDX tickers and IDX-IC sectors. Keys are
matched as prefixes of the normalized issuer name, longest key first.
"""
from __future__ import annotations

import re
from functools import lru_cache

FIN, CNC, CCY, ENE, BMAT = "Financials", "Consumer Non-Cyclicals", "Consumer Cyclicals", "Energy", "Basic Materials"
IND, INF, PROP, TECH, HLTH, TRN = ("Industrials", "Infrastructures", "Properties & Real Estate",
                                   "Technology", "Healthcare", "Transportation & Logistic")

# normalized name prefix -> (ticker, short name, sector)
IDX_SECURITIES: dict[str, tuple[str, str, str]] = {
    # Financials
    "bank central asia": ("BBCA", "Bank Central Asia", FIN),
    "bank rakyat indonesia": ("BBRI", "Bank Rakyat Indonesia", FIN),
    "bank mandiri": ("BMRI", "Bank Mandiri", FIN),
    "bank negara indonesia": ("BBNI", "Bank Negara Indonesia", FIN),
    "bank syariah indonesia": ("BRIS", "Bank Syariah Indonesia", FIN),
    "bank tabungan negara": ("BBTN", "Bank Tabungan Negara", FIN),
    "bank danamon": ("BDMN", "Bank Danamon", FIN),
    "bank cimb niaga": ("BNGA", "Bank CIMB Niaga", FIN),
    "bank ocbc nisp": ("NISP", "Bank OCBC NISP", FIN),
    "bank pan indonesia": ("PNBN", "Bank Pan Indonesia", FIN),
    "bank jago": ("ARTO", "Bank Jago", FIN),
    "bank neo commerce": ("BBYB", "Bank Neo Commerce", FIN),
    "bank btpn syariah": ("BTPS", "Bank BTPN Syariah", FIN),
    "bank pembangunan daerah jawa barat": ("BJBR", "Bank BJB", FIN),
    "bank pembangunan daerah jawa timur": ("BJTM", "Bank Jatim", FIN),
    "bank permata": ("BNLI", "Bank Permata", FIN),
    "bank maybank indonesia": ("BNII", "Bank Maybank Indonesia", FIN),
    "bfi finance indonesia": ("BFIN", "BFI Finance", FIN),
    "elang mahkota teknologi": ("EMTK", "Elang Mahkota Teknologi", TECH),
    # Consumer non-cyclicals
    "unilever indonesia": ("UNVR", "Unilever Indonesia", CNC),
    "indofood cbp sukses makmur": ("ICBP", "Indofood CBP", CNC),
    "indofood sukses makmur": ("INDF", "Indofood Sukses Makmur", CNC),
    "sumber alfaria trijaya": ("AMRT", "Sumber Alfaria Trijaya", CNC),
    "charoen pokphand indonesia": ("CPIN", "Charoen Pokphand Indonesia", CNC),
    "japfa comfeed indonesia": ("JPFA", "Japfa Comfeed", CNC),
    "mayora indah": ("MYOR", "Mayora Indah", CNC),
    "hanjaya mandala sampoerna": ("HMSP", "HM Sampoerna", CNC),
    "gudang garam": ("GGRM", "Gudang Garam", CNC),
    "industri jamu dan farmasi sido muncul": ("SIDO", "Sido Muncul", HLTH),
    "industri jamu & farmasi sido muncul": ("SIDO", "Sido Muncul", HLTH),
    "cisarua mountain dairy": ("CMRY", "Cisarua Mountain Dairy", CNC),
    "midi utama indonesia": ("MIDI", "Midi Utama Indonesia", CNC),
    "astra agro lestari": ("AALI", "Astra Agro Lestari", CNC),
    "perusahaan perkebunan london sumatra": ("LSIP", "London Sumatra", CNC),
    "salim ivomas pratama": ("SIMP", "Salim Ivomas", CNC),
    "ultrajaya milk": ("ULTJ", "Ultrajaya Milk", CNC),
    "nippon indosari corpindo": ("ROTI", "Nippon Indosari", CNC),
    "dharma satya nusantara": ("DSNG", "Dharma Satya Nusantara", CNC),
    "wilmar cahaya indonesia": ("CEKA", "Wilmar Cahaya", CNC),
    "malindo feedmill": ("MAIN", "Malindo Feedmill", CNC),
    # Consumer cyclicals
    "astra international": ("ASII", "Astra International", IND),
    "ace hardware indonesia": ("ACES", "Ace Hardware Indonesia", CCY),
    "aspirasi hidup indonesia": ("ACES", "Aspirasi Hidup Indonesia", CCY),
    "mitra adiperkasa": ("MAPI", "Mitra Adiperkasa", CCY),
    "map aktif adiperkasa": ("MAPA", "MAP Aktif Adiperkasa", CCY),
    "erajaya swasembada": ("ERAA", "Erajaya Swasembada", CCY),
    "matahari department store": ("LPPF", "Matahari Department Store", CCY),
    "media nusantara citra": ("MNCN", "Media Nusantara Citra", CCY),
    "surya citra media": ("SCMA", "Surya Citra Media", CCY),
    "adi sarana armada": ("ASSA", "Adi Sarana Armada", TRN),
    "mitra keluarga karyasehat": ("MIKA", "Mitra Keluarga", HLTH),
    "selamat sempurna": ("SMSM", "Selamat Sempurna", CCY),
    "astra otoparts": ("AUTO", "Astra Otoparts", CCY),
    "map boga adiperkasa": ("MAPB", "MAP Boga Adiperkasa", CCY),
    # Energy
    "adaro energy": ("ADRO", "Adaro Energy", ENE),
    "alamtri resources indonesia": ("ADRO", "Alamtri Resources (ex-Adaro)", ENE),
    "adaro minerals indonesia": ("ADMR", "Adaro Minerals", ENE),
    "alamtri minerals indonesia": ("ADMR", "Alamtri Minerals (ex-Adaro Min.)", ENE),
    "adaro andalan indonesia": ("AADI", "Adaro Andalan Indonesia", ENE),
    "bukit asam": ("PTBA", "Bukit Asam", ENE),
    "indo tambangraya megah": ("ITMG", "Indo Tambangraya Megah", ENE),
    "united tractors": ("UNTR", "United Tractors", IND),
    "perusahaan gas negara": ("PGAS", "Perusahaan Gas Negara", ENE),
    "medco energi internasional": ("MEDC", "Medco Energi", ENE),
    "akr corporindo": ("AKRA", "AKR Corporindo", ENE),
    "harum energy": ("HRUM", "Harum Energy", ENE),
    "indika energy": ("INDY", "Indika Energy", ENE),
    "bumi resources minerals": ("BRMS", "Bumi Resources Minerals", BMAT),
    "bumi resources": ("BUMI", "Bumi Resources", ENE),
    "golden energy mines": ("GEMS", "Golden Energy Mines", ENE),
    "surya esa perkasa": ("ESSA", "Surya Esa Perkasa", BMAT),
    "elnusa": ("ELSA", "Elnusa", ENE),
    "petrindo jaya kreasi": ("CUAN", "Petrindo Jaya Kreasi", ENE),
    "barito renewables energy": ("BREN", "Barito Renewables", INF),
    # Basic materials
    "aneka tambang": ("ANTM", "Aneka Tambang", BMAT),
    "merdeka copper gold": ("MDKA", "Merdeka Copper Gold", BMAT),
    "merdeka battery materials": ("MBMA", "Merdeka Battery Materials", BMAT),
    "vale indonesia": ("INCO", "Vale Indonesia", BMAT),
    "amman mineral internasional": ("AMMN", "Amman Mineral", BMAT),
    "trimegah bangun persada": ("NCKL", "Trimegah Bangun Persada", BMAT),
    "archi indonesia": ("ARCI", "Archi Indonesia", BMAT),
    "barito pacific": ("BRPT", "Barito Pacific", BMAT),
    "chandra asri": ("TPIA", "Chandra Asri", BMAT),
    "semen indonesia": ("SMGR", "Semen Indonesia", BMAT),
    "indocement tunggal prakarsa": ("INTP", "Indocement", BMAT),
    "indah kiat pulp & paper": ("INKP", "Indah Kiat Pulp & Paper", BMAT),
    "indah kiat pulp and paper": ("INKP", "Indah Kiat Pulp & Paper", BMAT),
    "pabrik kertas tjiwi kimia": ("TKIM", "Tjiwi Kimia", BMAT),
    "timah": ("TINS", "Timah", BMAT),
    "avia avian": ("AVIA", "Avia Avian", BMAT),
    "pt avia avian": ("AVIA", "Avia Avian", BMAT),
    # Industrials / infrastructure
    "telkom indonesia": ("TLKM", "Telkom Indonesia", INF),
    "telekomunikasi indonesia": ("TLKM", "Telkom Indonesia", INF),
    "sarana menara nusantara": ("TOWR", "Sarana Menara Nusantara", INF),
    "tower bersama infrastructure": ("TBIG", "Tower Bersama", INF),
    "dayamitra telekomunikasi": ("MTEL", "Dayamitra (Mitratel)", INF),
    "xl axiata": ("EXCL", "XL Axiata", INF),
    "xlsmart telecom sejahtera": ("EXCL", "XLSmart Telecom", INF),
    "indosat": ("ISAT", "Indosat Ooredoo Hutchison", INF),
    "jasa marga": ("JSMR", "Jasa Marga", INF),
    "waskita karya": ("WSKT", "Waskita Karya", INF),
    "wijaya karya": ("WIKA", "Wijaya Karya", INF),
    "adhi karya": ("ADHI", "Adhi Karya", INF),
    "jaya real property": ("JRPT", "Jaya Real Property", PROP),
    "smartfren telecom": ("FREN", "Smartfren", INF),
    "link net": ("LINK", "Link Net", INF),
    "sarana multi infrastruktur": ("SMII", "Sarana Multi Infrastruktur", INF),
    "impack pratama industri": ("IMPC", "Impack Pratama", IND),
    "arwana citramulia": ("ARNA", "Arwana Citramulia", IND),
    "blue bird": ("BIRD", "Blue Bird", TRN),
    "samudera indonesia": ("SMDR", "Samudera Indonesia", TRN),
    # Property
    "ciputra development": ("CTRA", "Ciputra Development", PROP),
    "bumi serpong damai": ("BSDE", "Bumi Serpong Damai", PROP),
    "pakuwon jati": ("PWON", "Pakuwon Jati", PROP),
    "summarecon agung": ("SMRA", "Summarecon Agung", PROP),
    "lippo karawaci": ("LPKR", "Lippo Karawaci", PROP),
    "puradelta lestari": ("DMAS", "Puradelta Lestari", PROP),
    "pantai indah kapuk dua": ("PANI", "Pantai Indah Kapuk Dua", PROP),
    # Technology
    "goto gojek tokopedia": ("GOTO", "GoTo Gojek Tokopedia", TECH),
    "bukalapak.com": ("BUKA", "Bukalapak", TECH),
    "bukalapakcom": ("BUKA", "Bukalapak", TECH),
    "bukalapak": ("BUKA", "Bukalapak", TECH),
    "metrodata electronics": ("MTDL", "Metrodata Electronics", TECH),
    "dci indonesia": ("DCII", "DCI Indonesia", TECH),
    "indointernet": ("EDGE", "Indointernet", TECH),
    # Healthcare
    "kalbe farma": ("KLBF", "Kalbe Farma", HLTH),
    "medikaloka hermina": ("HEAL", "Medikaloka Hermina", HLTH),
    "siloam international hospitals": ("SILO", "Siloam Hospitals", HLTH),
    "prodia widyahusada": ("PRDA", "Prodia Widyahusada", HLTH),
    "kimia farma": ("KAEF", "Kimia Farma", HLTH),
    "essa industries indonesia": ("ESSA", "ESSA Industries", BMAT),
    "petrosea": ("PTRO", "Petrosea", ENE),
    "energi mega persada": ("ENRG", "Energi Mega Persada", ENE),
    "rukun raharja": ("RAJA", "Rukun Raharja", ENE),
    "raharja energi cepu": ("RATU", "Raharja Energi Cepu", ENE),
    "sawit sumbermas sarana": ("SSMS", "Sawit Sumbermas Sarana", CNC),
    "triputra agro persada": ("TAPG", "Triputra Agro Persada", CNC),
    "sariguna primatirta": ("CLEO", "Sariguna Primatirta", CNC),
    "solusi sinergi digital": ("WIFI", "Solusi Sinergi Digital", TECH),
    "bank aladin syariah": ("BANK", "Bank Aladin Syariah", FIN),
    "panin financial": ("PNLF", "Panin Financial", FIN),
    "pp presisi": ("PPRE", "PP Presisi", INF),
    "pp": ("PTPP", "PP (Persero)", INF),
    "transcoal pacific": ("TCPI", "Transcoal Pacific", TRN),
    "multi bintang indonesia": ("MLBI", "Multi Bintang Indonesia", CNC),
    "surya semesta internusa": ("SSIA", "Surya Semesta Internusa", PROP),
    "sentul city": ("BKSL", "Sentul City", PROP),
    "bank bukopin": ("BBKP", "Bank Bukopin", FIN),
    "bank kb bukopin": ("BBKP", "Bank KB Bukopin", FIN),
    "ramayana lestari sentosa": ("RALS", "Ramayana Lestari", CCY),
    "mnc digital entertainment": ("MSIN", "MNC Digital Entertainment", CCY),
    "metro healthcare indonesia": ("CARE", "Metro Healthcare", HLTH),
    "alam sutera realty": ("ASRI", "Alam Sutera Realty", PROP),
    "pacific strategic financial": ("APIC", "Pacific Strategic Financial", FIN),
    "berkah beton sadaya": ("BEBS", "Berkah Beton Sadaya", BMAT),
    "citra marga nusaphala persada": ("CMNP", "Citra Marga Nusaphala", INF),
    "bni": ("BBNI", "Bank Negara Indonesia", FIN),
    "antam": ("ANTM", "Aneka Tambang", BMAT),
    "integra indocabinet": ("WOOD", "Integra Indocabinet", CCY),
    "global mediacom": ("BMTR", "Global Mediacom", CCY),
    "kawasan industri jababeka": ("KIJA", "Kawasan Industri Jababeka", PROP),
    "uni-charm indonesia": ("UCID", "Uni-Charm Indonesia", CNC),
    "kresna graha investama": ("KREN", "Kresna Graha Investama", FIN),
    "md entertainment": ("FILM", "MD Entertainment", CCY),
    "krakatau steel": ("KRAS", "Krakatau Steel", BMAT),
    "tunas baru lampung": ("TBLA", "Tunas Baru Lampung", CNC),
    "pertamina geothermal energy": ("PGEO", "Pertamina Geothermal", INF),
    "mastersystem infotama": ("MSTI", "Mastersystem Infotama", TECH),
    "waskita beton precast": ("WSBP", "Waskita Beton Precast", BMAT),
    "bank raya indonesia": ("AGRO", "Bank Raya Indonesia", FIN),
    "mitra pinasthika mustika": ("MPMX", "Mitra Pinasthika Mustika", CCY),
    "buana lintas lautan": ("BULL", "Buana Lintas Lautan", ENE),
    "bayan resources": ("BYAN", "Bayan Resources", ENE),
    "bisi international": ("BISI", "BISI International", CNC),
    "delta dunia makmur": ("DOID", "Delta Dunia Makmur", ENE),
    "lippo cikarang": ("LPCK", "Lippo Cikarang", PROP),
    "bank tabungan pensiunan nasional syariah": ("BTPS", "Bank BTPN Syariah", FIN),
    # Holdings / investment
    "saratoga investama sedaya": ("SRTG", "Saratoga Investama", FIN),
    "dian swastatika sentosa": ("DSSA", "Dian Swastatika Sentosa", ENE),
}

_STOPWORDS = re.compile(r"\b(pt|tbk|persero|perseroan|terbuka|the|ord|shs|registered|class [ab])\b")


def normalize_name(name: str | None) -> str:
    """Lowercase, strip corporate suffixes and punctuation for cross-source matching."""
    if not name:
        return ""
    s = name.lower().replace("&amp;", "&")
    s = re.sub(r"[(),.'\"/]", " ", s)
    s = _STOPWORDS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


_KEYS = sorted(((normalize_name(k), v) for k, v in IDX_SECURITIES.items()),
               key=lambda kv: len(kv[0]), reverse=True)


@lru_cache(maxsize=4096)
def resolve(issuer_name: str | None) -> tuple[str, str, str] | None:
    """Return (ticker, short_name, sector) for an issuer name, or None if unmapped."""
    n = normalize_name(issuer_name)
    if not n:
        return None
    for key, val in _KEYS:
        if n == key or n.startswith(key + " "):
            return val
    return None
