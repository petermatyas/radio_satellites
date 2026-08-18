"""Országkódok: zászló és magyar név.

A SatNOGS katalógus ISO 3166-1 alfa-2 kódokat ad a "countries" mezőben,
vesszővel elválasztva, ha több ország is részt vett (pl. az ISS-nél "RU,US").

A zászlót nem képként tároljuk: a két betűből számoljuk ki a hozzájuk tartozó
Unicode regionális jelzőpárt. Így nincs se letöltendő ikonkészlet, se külső
kérés, és a jel a szöveggel együtt méreteződik. Windowson a böngészők nem
rajzolnak zászlót, hanem magát a két betűt mutatják — ez is olvasható marad.

A neveket a CLDR magyar adataiból generáltuk (babel), de itt beégetve
tároljuk: futásidőben nem akartunk új függőséget a konténerbe.
"""

# Regionális jelző szimbólumok kezdőpontja: 'A' -> U+1F1E6.
_FLAG_BASE = 0x1F1E6
_A = ord("A")

NAMES = {
    "AC": "Ascension-sziget", "AD": "Andorra",
    "AE": "Egyesült Arab Emírségek", "AF": "Afganisztán",
    "AG": "Antigua és Barbuda", "AI": "Anguilla", "AL": "Albánia",
    "AM": "Örményország", "AO": "Angola", "AQ": "Antarktisz",
    "AR": "Argentína", "AS": "Amerikai Szamoa", "AT": "Ausztria",
    "AU": "Ausztrália", "AW": "Aruba", "AX": "Åland-szigetek",
    "AZ": "Azerbajdzsán", "BA": "Bosznia-Hercegovina", "BB": "Barbados",
    "BD": "Banglades", "BE": "Belgium", "BF": "Burkina Faso",
    "BG": "Bulgária", "BH": "Bahrein", "BI": "Burundi", "BJ": "Benin",
    "BL": "Saint-Barthélemy", "BM": "Bermuda", "BN": "Brunei",
    "BO": "Bolívia", "BQ": "Holland Karib-térség", "BR": "Brazília",
    "BS": "Bahama-szigetek", "BT": "Bhután", "BV": "Bouvet-sziget",
    "BW": "Botswana", "BY": "Belarusz", "BZ": "Belize", "CA": "Kanada",
    "CC": "Kókusz (Keeling)-szigetek", "CD": "Kongó – Kinshasa",
    "CF": "Közép-afrikai Köztársaság", "CG": "Kongó – Brazzaville",
    "CH": "Svájc", "CI": "Elefántcsontpart", "CK": "Cook-szigetek",
    "CL": "Chile", "CM": "Kamerun", "CN": "Kína", "CO": "Kolumbia",
    "CP": "Clipperton-sziget", "CQ": "Sark", "CR": "Costa Rica", "CU": "Kuba",
    "CV": "Zöld-foki Köztársaság", "CW": "Curaçao", "CX": "Karácsony-sziget",
    "CY": "Ciprus", "CZ": "Csehország", "DE": "Németország",
    "DG": "Diego Garcia", "DJ": "Dzsibuti", "DK": "Dánia", "DM": "Dominika",
    "DO": "Dominikai Köztársaság", "DZ": "Algéria", "EA": "Ceuta és Melilla",
    "EC": "Ecuador", "EE": "Észtország", "EG": "Egyiptom",
    "EH": "Nyugat-Szahara", "ER": "Eritrea", "ES": "Spanyolország",
    "ET": "Etiópia", "FI": "Finnország", "FJ": "Fidzsi",
    "FK": "Falkland-szigetek", "FM": "Mikronézia", "FO": "Feröer szigetek",
    "FR": "Franciaország", "GA": "Gabon", "GB": "Egyesült Királyság",
    "GD": "Grenada", "GE": "Grúzia", "GF": "Francia Guyana", "GG": "Guernsey",
    "GH": "Ghána", "GI": "Gibraltár", "GL": "Grönland", "GM": "Gambia",
    "GN": "Guinea", "GP": "Guadeloupe", "GQ": "Egyenlítői-Guinea",
    "GR": "Görögország", "GS": "Déli-Georgia és Déli-Sandwich-szigetek",
    "GT": "Guatemala", "GU": "Guam", "GW": "Bissau-Guinea", "GY": "Guyana",
    "HK": "Hongkong KKT", "HM": "Heard-sziget és McDonald-szigetek",
    "HN": "Honduras", "HR": "Horvátország", "HT": "Haiti",
    "HU": "Magyarország", "IC": "Kanári-szigetek", "ID": "Indonézia",
    "IE": "Írország", "IL": "Izrael", "IM": "Man-sziget", "IN": "India",
    "IO": "Brit Indiai-óceáni Terület", "IQ": "Irak", "IR": "Irán",
    "IS": "Izland", "IT": "Olaszország", "JE": "Jersey", "JM": "Jamaica",
    "JO": "Jordánia", "JP": "Japán", "KE": "Kenya", "KG": "Kirgizisztán",
    "KH": "Kambodzsa", "KI": "Kiribati", "KM": "Comore-szigetek",
    "KN": "Saint Kitts és Nevis", "KP": "Észak-Korea", "KR": "Dél-Korea",
    "KW": "Kuvait", "KY": "Kajmán-szigetek", "KZ": "Kazahsztán",
    "LA": "Laosz", "LB": "Libanon", "LC": "Saint Lucia",
    "LI": "Liechtenstein", "LK": "Srí Lanka", "LR": "Libéria",
    "LS": "Lesotho", "LT": "Litvánia", "LU": "Luxemburg", "LV": "Lettország",
    "LY": "Líbia", "MA": "Marokkó", "MC": "Monaco", "MD": "Moldova",
    "ME": "Montenegró", "MF": "Saint Martin", "MG": "Madagaszkár",
    "MH": "Marshall-szigetek", "MK": "Észak-Macedónia", "ML": "Mali",
    "MM": "Mianmar", "MN": "Mongólia", "MO": "Makaó KKT",
    "MP": "Északi Mariana-szigetek", "MQ": "Martinique", "MR": "Mauritánia",
    "MS": "Montserrat", "MT": "Málta", "MU": "Mauritius",
    "MV": "Maldív-szigetek", "MW": "Malawi", "MX": "Mexikó", "MY": "Malajzia",
    "MZ": "Mozambik", "NA": "Namíbia", "NC": "Új-Kaledónia", "NE": "Niger",
    "NF": "Norfolk-sziget", "NG": "Nigéria", "NI": "Nicaragua",
    "NL": "Hollandia", "NO": "Norvégia", "NP": "Nepál", "NR": "Nauru",
    "NU": "Niue", "NZ": "Új-Zéland", "OM": "Omán", "PA": "Panama",
    "PE": "Peru", "PF": "Francia Polinézia", "PG": "Pápua Új-Guinea",
    "PH": "Fülöp-szigetek", "PK": "Pakisztán", "PL": "Lengyelország",
    "PM": "Saint-Pierre és Miquelon", "PN": "Pitcairn-szigetek",
    "PR": "Puerto Rico", "PS": "Palesztin Autonómia", "PT": "Portugália",
    "PW": "Palau", "PY": "Paraguay", "QA": "Katar", "RE": "Réunion",
    "RO": "Románia", "RS": "Szerbia", "RU": "Oroszország", "RW": "Ruanda",
    "SA": "Szaúd-Arábia", "SB": "Salamon-szigetek",
    "SC": "Seychelle-szigetek", "SD": "Szudán", "SE": "Svédország",
    "SG": "Szingapúr", "SH": "Szent Ilona", "SI": "Szlovénia",
    "SJ": "Svalbard és Jan Mayen", "SK": "Szlovákia", "SL": "Sierra Leone",
    "SM": "San Marino", "SN": "Szenegál", "SO": "Szomália", "SR": "Suriname",
    "SS": "Dél-Szudán", "ST": "São Tomé és Príncipe", "SV": "Salvador",
    "SX": "Sint Maarten", "SY": "Szíria", "SZ": "Szváziföld",
    "TA": "Tristan da Cunha", "TC": "Turks- és Caicos-szigetek", "TD": "Csád",
    "TF": "Francia Déli Területek", "TG": "Togo", "TH": "Thaiföld",
    "TJ": "Tádzsikisztán", "TK": "Tokelau", "TL": "Kelet-Timor",
    "TM": "Türkmenisztán", "TN": "Tunézia", "TO": "Tonga",
    "TR": "Törökország", "TT": "Trinidad és Tobago", "TV": "Tuvalu",
    "TW": "Tajvan", "TZ": "Tanzánia", "UA": "Ukrajna", "UG": "Uganda",
    "UM": "Az USA lakatlan külbirtokai", "US": "Egyesült Államok",
    "UY": "Uruguay", "UZ": "Üzbegisztán", "VA": "Vatikán",
    "VC": "Saint Vincent és a Grenadine-szigetek", "VE": "Venezuela",
    "VG": "Brit Virgin-szigetek", "VI": "Amerikai Virgin-szigetek",
    "VN": "Vietnám", "VU": "Vanuatu", "WF": "Wallis és Futuna",
    "WS": "Szamoa", "XK": "Koszovó", "YE": "Jemen", "YT": "Mayotte",
    "ZA": "Dél-afrikai Köztársaság", "ZM": "Zambia", "ZW": "Zimbabwe"
}


def flag(code):
    """Kétbetűs országkód -> zászló emoji. Ismeretlen alakra üres sztring."""
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(_FLAG_BASE + ord(ch) - _A) for ch in code)


def name(code):
    """Magyar országnév; ha nem ismerjük, marad a nyers kód."""
    code = (code or "").strip().upper()
    return NAMES.get(code, code)


def badges(raw):
    """A "countries" mező -> [{"code", "flag", "name"}].

    Üres vagy ismeretlen alakú bemenetre üres lista, hogy a sablon egyszerűen
    végig tudjon menni rajta. A sorrendet megtartjuk (a SatNOGS-ban az első
    kód a fő közreműködő), és az ismétlődéseket kiszűrjük.
    """
    out, seen = [], set()
    for part in (raw or "").split(","):
        code = part.strip().upper()
        if not flag(code) or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "flag": flag(code), "name": name(code)})
    return out
