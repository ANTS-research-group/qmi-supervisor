"""Pure, conservative radio presentation helpers; no GI, hardware or network I/O.

Numeric QMI band codes below are explicit upstream API values, not arithmetic
band-number guesses:
https://raw.githubusercontent.com/linux-mobile-broadband/libqmi/main/src/libqmi-glib/qmi-enums-nas.h
NR reference raster: ETSI TS 138 104 V17.14.0, 5.4.2.1 (MHz).
https://www.etsi.org/deliver/etsi_TS/138100_138199/138104/17.14.00_60/ts_138104v171400p.pdf
LTE DL rows are a conservative subset of TS 136 101 V17.8.0, table 5.7.3-1.
Unlisted ranges remain unavailable rather than being extrapolated.
https://www.etsi.org/deliver/etsi_ts/136100_136199/136101/17.08.00_60/ts_136101v170800p.pdf
"""
from collections.abc import Mapping
import math
import re


QMI_NR_BANDS = {
    250: 1, 251: 2, 252: 3, 253: 5, 254: 7, 255: 8, 256: 20,
    257: 28, 258: 38, 259: 41, 260: 50, 261: 51, 262: 66,
    263: 70, 264: 71, 265: 74, 266: 75, 267: 76, 268: 77,
    269: 78, 270: 79, 271: 80, 272: 81, 273: 82, 274: 83,
    275: 84, 276: 85, 277: 257, 278: 258, 279: 259, 280: 260,
    281: 261, 282: 12, 283: 25, 284: 34, 285: 39, 286: 40,
    287: 65, 288: 86, 289: 48, 290: 14, 291: 13, 292: 18,
    293: 26, 294: 30, 295: 29, 296: 53, 297: 46, 298: 91,
    299: 92, 300: 93, 301: 94, 315: 100, 316: 101,
}
QMI_LTE_BANDS = {
    120: 1, 121: 2, 122: 3, 123: 4, 124: 5, 125: 6, 126: 7,
    127: 8, 128: 9, 129: 10, 130: 11, 131: 12, 132: 13,
    133: 14, 134: 17, 143: 18, 144: 19, 145: 20, 146: 21,
    152: 23, 147: 24, 148: 25, 153: 26, 164: 27, 158: 28,
    159: 29, 160: 30, 165: 31, 154: 32, 135: 33, 136: 34,
    137: 35, 138: 36, 139: 37, 140: 38, 141: 39, 142: 40,
    149: 41, 150: 42, 151: 43, 163: 46, 166: 47, 167: 48,
    161: 66, 168: 71, 155: 125, 156: 126, 157: 127, 162: 250,
}

# QmiNasDLBandwidth (including values added upstream in libqmi 1.36).
# Get RF Band Information / Bandwidth List uses this exact enum, not a
# separate NR bandwidth index. In particular, code 9 is NR 20 MHz; 17 is 100.
# https://raw.githubusercontent.com/linux-mobile-broadband/libqmi/main/data/qmi-service-nas.json
QMI_DL_BANDWIDTHS = {
    0: ('lte', 1.4), 1: ('lte', 3), 2: ('lte', 5), 3: ('lte', 10),
    4: ('lte', 15), 5: ('lte', 20),
    6: ('nr', 5), 7: ('nr', 10), 8: ('nr', 15), 9: ('nr', 20),
    10: ('nr', 25), 11: ('nr', 30), 12: ('nr', 40), 13: ('nr', 50),
    14: ('nr', 60), 15: ('nr', 80), 16: ('nr', 90), 17: ('nr', 100),
    18: ('nr', 200), 19: ('nr', 400), 20: ('gsm', 0.2),
    21: ('td-scdma', 1.6), 22: ('umts', 5), 23: ('umts', 10), 24: ('nr', 70),
}

# (band, first DL EARFCN/NOffs-DL, last DL EARFCN, FDL_low in kHz).
# Integer kHz arithmetic avoids a binary-float artefact such as 3449.2799999.
LTE_DL_RANGES = (
    (1, 0, 599, 2110000), (2, 600, 1199, 1930000), (3, 1200, 1949, 1805000),
    (4, 1950, 2399, 2110000), (5, 2400, 2649, 869000), (6, 2650, 2749, 875000),
    (7, 2750, 3449, 2620000), (8, 3450, 3799, 925000), (9, 3800, 4149, 1844900),
    (10, 4150, 4749, 2110000), (11, 4750, 4949, 1475900), (12, 5010, 5179, 729000),
    (13, 5180, 5279, 746000), (14, 5280, 5379, 758000), (17, 5730, 5849, 734000),
    (18, 5850, 5999, 860000), (19, 6000, 6149, 875000), (20, 6150, 6449, 791000),
    (21, 6450, 6599, 1495900), (22, 6600, 7399, 3510000), (23, 7500, 7699, 2180000),
    (24, 7700, 8039, 1525000), (25, 8040, 8689, 1930000), (26, 8690, 9039, 859000),
    (27, 9040, 9209, 852000), (28, 9210, 9659, 758000), (29, 9660, 9769, 717000),
    (30, 9770, 9869, 2350000), (31, 9870, 9919, 462500), (32, 9920, 10359, 1452000),
    (33, 36000, 36199, 1900000), (34, 36200, 36349, 2010000),
    (35, 36350, 36949, 1850000), (36, 36950, 37549, 1930000),
    (37, 37550, 37749, 1910000), (38, 37750, 38249, 2570000),
    (39, 38250, 38649, 1880000), (40, 38650, 39649, 2300000),
    (41, 39650, 41589, 2496000), (42, 41590, 43589, 3400000),
    (43, 43590, 45589, 3600000), (44, 45590, 46589, 703000),
    (45, 46590, 46789, 1447000), (46, 46790, 54539, 5150000),
    (47, 54540, 55239, 5855000), (48, 55240, 56739, 3550000),
    (49, 56740, 58239, 3550000), (50, 58240, 59089, 1432000),
    (51, 59090, 59139, 1427000), (52, 59140, 60139, 3300000),
    (65, 65536, 66435, 2110000), (66, 66436, 67335, 2110000),
    (67, 67336, 67535, 738000), (68, 67536, 67835, 753000),
    (69, 67836, 68335, 2570000), (70, 68336, 68585, 1995000),
    (71, 68586, 68935, 617000),
)

NR_FREQUENCY_LABEL = 'NR reference frequency (calculated)'
LTE_FREQUENCY_LABEL = 'LTE downlink frequency (calculated)'


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _items(value):
    return value if isinstance(value, (list, tuple)) else ()


def _integer(value, minimum=0, maximum=None):
    return (int(value) if isinstance(value, int) and not isinstance(value, bool)
            and value >= minimum and (maximum is None or value <= maximum) else None)


def _number(value):
    return value if type(value) is int or (type(value) is float and math.isfinite(value)) else None


def normalize_rat(value):
    if not isinstance(value, str) or value.lower() in {'none', 'unknown', ''}:
        return None
    return {'5gnr': 'nr', 'nr5g': 'nr', '5g': 'nr', 'eutran': 'lte'}.get(value.lower(), value.lower())


def active_band_label(raw):
    """Fallback for a GI version whose enum-to-string table predates NR."""
    raw = _integer(raw)
    if raw in QMI_NR_BANDS:
        return 'nr5g-' + str(QMI_NR_BANDS[raw])
    if raw in QMI_LTE_BANDS:
        return 'eutran-' + str(QMI_LTE_BANDS[raw])
    return None


def bandwidth_mhz(raw, rat):
    """Decode only documented enum values consistent with the observed RAT."""
    decoded = QMI_DL_BANDWIDTHS.get(_integer(raw))
    return decoded[1] if decoded and normalize_rat(rat) == decoded[0] else None


def _observed_band(raw, label, rat):
    if rat == 'nr' and raw in QMI_NR_BANDS:
        return 'n' + str(QMI_NR_BANDS[raw])
    if rat == 'lte' and raw in QMI_LTE_BANDS:
        return 'B' + str(QMI_LTE_BANDS[raw])
    # A decoded GI label is evidence too, including bands added after our table.
    pattern = r'(?:nr5g-|5gnr-|n)([0-9]+)' if rat == 'nr' else r'(?:eutran-|b)([0-9]+)' if rat == 'lte' else None
    match = re.fullmatch(pattern, label.lower()) if pattern and isinstance(label, str) else None
    return (('n' if rat == 'nr' else 'B') + match[1]) if match else None


def nr_reference_frequency_mhz(channel):
    channel = _integer(channel, maximum=3279165)
    if channel is None:
        return None
    if channel < 600000:
        frequency_khz = channel * 5
    elif channel < 2016667:
        frequency_khz = 3000000 + (channel - 600000) * 15
    else:
        frequency_khz = 24250080 + (channel - 2016667) * 60
    return frequency_khz / 1000


def lte_downlink_frequency_mhz(channel):
    channel = _integer(channel, maximum=262143)
    if channel is None:
        return None
    matches = [low + (channel-first) * 100 for _, first, last, low in LTE_DL_RANGES if first <= channel <= last]
    return matches[0] / 1000 if len(matches) == 1 else None


def format_plmn(mcc, mnc, includes_pcs_digit=None):
    """Do not guess whether an integer MNC 10 means 10 or 010."""
    mcc, mnc = _integer(mcc, maximum=999), _integer(mnc, maximum=999)
    if mcc is None or mnc is None:
        return None
    if includes_pcs_digit is None:
        if mnc < 100:
            return None
        includes_pcs_digit = True
    if type(includes_pcs_digit) is not bool or (not includes_pcs_digit and mnc >= 100):
        return None
    return f'{mcc:03d}{mnc:03d}' if includes_pcs_digit else f'{mcc:03d}{mnc:02d}'


def _plmn(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]{5,6}', value) else None


def _frequency(rat, channel):
    if rat == 'nr':
        return dict(channel_type='NR-ARFCN', frequency_mhz=nr_reference_frequency_mhz(channel),
                    frequency_label=NR_FREQUENCY_LABEL)
    if rat == 'lte':
        return dict(channel_type='EARFCN', frequency_mhz=lte_downlink_frequency_mhz(channel),
                    frequency_label=LTE_FREQUENCY_LABEL)
    return dict(channel_type=None, frequency_mhz=None, frequency_label=None)


def radio_summary(sample):
    """Normalize one acquired sample without inferring band, SA/NSA or CA roles.

    Frequency conversions carry their source. RF bands and bandwidths remain
    independent lists: neither array ordering nor RAT alone identifies a carrier.
    """
    sample = _mapping(sample)
    dynamic = _mapping(sample.get('dynamic'))
    requests = _mapping(_mapping(sample.get('collection')).get('requests'))
    fallback_time = _mapping(sample.get('timing')).get('acquired_end_utc') or sample.get('end_datetime')

    def observed_at(request):
        return _mapping(requests.get(request)).get('received_at_utc') or fallback_time

    bands, bandwidths, cells, neighbors = [], [], [], []
    rats = []
    def add_rat(value):
        rat = normalize_rat(value)
        if rat is not None and rat not in rats:
            rats.append(rat)
        return rat

    for value in _items(dynamic.get('nas_value_serving_system_radio_interfaces')):
        add_rat(value)
    if _number(dynamic.get('nas_value_5g_signal_strength_rsrp')) is not None:
        add_rat('nr')
    if _number(dynamic.get('nas_value_lte_rsrp')) is not None:
        add_rat('lte')
    extended = [item for item in _items(dynamic.get('nas_value_rf_bands_extended')) if isinstance(item, Mapping)]
    extended_rats = {normalize_rat(item.get('radio_interface')) for item in extended}
    for source, entries in (
        ('nas_value_rf_bands_extended', extended),
        ('nas_value_rf_bands', _items(dynamic.get('nas_value_rf_bands'))),
    ):
        for item in entries:
            item = _mapping(item)
            rat = normalize_rat(item.get('radio_interface'))
            if not item or (source == 'nas_value_rf_bands' and rat in extended_rats):
                continue
            add_rat(item.get('radio_interface'))
            raw = _integer(item.get('active_band_class_raw'))
            label = item.get('active_band_class') or active_band_label(raw)
            raw_channel = _integer(item.get('active_channel'), maximum=0xFFFFFFFF)
            # The old TLV truncates NR channels above 65535. It can still report
            # a band, but it must never supply NR frequency or a cell channel.
            channel = None if rat == 'nr' and source == 'nas_value_rf_bands' else raw_channel
            if rat == 'nr' and nr_reference_frequency_mhz(channel) is None:
                channel = None
            bands.append(dict(rat=rat, band=_observed_band(raw, label, rat), raw=raw, label=label,
                              channel=channel, raw_channel=raw_channel, **_frequency(rat, channel),
                              source=source, observed_at=observed_at('nas.get_rf_band_information')))
    for item in _items(dynamic.get('nas_value_rf_bandwidths')):
        item = _mapping(item)
        if item:
            rat = add_rat(item.get('radio_interface'))
            raw = _integer(item.get('bandwidth_raw'))
            bandwidths.append(dict(rat=rat, label=item.get('bandwidth'), raw=raw,
                                   bandwidth_mhz=bandwidth_mhz(raw, rat), unit='MHz',
                                   radio_interface_raw=_integer(item.get('radio_interface_raw')),
                                   source='nas_value_rf_bandwidths',
                                   observed_at=observed_at('nas.get_rf_band_information')))

    current_plmn = _plmn(dynamic.get('nas_value_current_plmn')) or format_plmn(
        dynamic.get('nas_value_current_plmn_mcc'), dynamic.get('nas_value_current_plmn_mnc'),
        dynamic.get('nas_value_current_plmn_mnc_is_3_digits'))
    nr_prefix = 'nas_value_nr5g_cell_information_'
    nr_identity = any(dynamic.get(nr_prefix+key) is not None for key in ('physical_cell_id', 'global_cell_id', 'plmn'))
    nr_channel = _integer(dynamic.get('nas_value_nr5g_arfcn'), maximum=3279165)
    channel_source = 'nas_value_nr5g_arfcn' if nr_channel is not None else None
    if nr_channel is None:
        candidates = {item['channel'] for item in bands if item['rat'] == 'nr' and item['channel'] is not None}
        if len(candidates) == 1:
            nr_channel = candidates.pop()
            channel_source = 'nas_value_rf_bands_extended'
    if nr_identity or nr_channel is not None:
        add_rat('nr')
        cells.append(dict(rat='nr', plmn=_plmn(dynamic.get(nr_prefix+'plmn')) or current_plmn,
                          pci=_integer(dynamic.get(nr_prefix+'physical_cell_id'), maximum=1007),
                          tac=_integer(dynamic.get(nr_prefix+'tracking_area_code')),
                          cell_id=_integer(dynamic.get(nr_prefix+'global_cell_id')),
                          channel=nr_channel, **_frequency('nr', nr_channel), channel_source=channel_source,
                          source='nas.get_cell_location_info' if nr_identity or channel_source == 'nas_value_nr5g_arfcn'
                          else 'nas.get_rf_band_information',
                          observed_at=observed_at('nas.get_cell_location_info' if nr_identity or
                                                 channel_source == 'nas_value_nr5g_arfcn' else 'nas.get_rf_band_information')))

    def neighbor(item, channel, relation):
        item = _mapping(item)
        return dict(rat='lte', plmn=None, pci=_integer(item.get('physical_cell_id'), maximum=503),
                    tac=None, cell_id=None, channel=channel, **_frequency('lte', channel), relation=relation,
                    rsrp=_number(item.get('rsrp')), rsrq=_number(item.get('rsrq')), rssi=_number(item.get('rssi')),
                    source='nas.get_cell_location_info', observed_at=observed_at('nas.get_cell_location_info'))

    intra = _mapping(dynamic.get('nas_value_lte_intrafrequency'))
    if intra:
        add_rat('lte')
        channel = _integer(intra.get('earfcn'), maximum=262143)
        serving_id = _integer(intra.get('serving_cell_id'), maximum=503)
        entries = [_mapping(item) for item in _items(intra.get('cells'))]
        serving = [item for item in entries if serving_id is not None and item.get('physical_cell_id') == serving_id]
        cell = dict(rat='lte', plmn=_plmn(intra.get('plmn')) or current_plmn, pci=serving_id,
                    tac=_integer(intra.get('tracking_area_code')), cell_id=_integer(intra.get('global_cell_id')),
                    channel=channel, **_frequency('lte', channel), channel_source='nas_value_lte_intrafrequency.earfcn',
                    source='nas.get_cell_location_info', observed_at=observed_at('nas.get_cell_location_info'))
        if len(serving) == 1:
            cell.update({key: _number(serving[0].get(key)) for key in ('rsrp', 'rsrq', 'rssi')})
        cells.append(cell)
        neighbors.extend(neighbor(item, channel, 'intra') for item in entries
                         if serving_id is None or item.get('physical_cell_id') != serving_id)
    inter = _mapping(dynamic.get('nas_value_lte_interfrequency'))
    for frequency in _items(inter.get('frequencies')):
        frequency = _mapping(frequency)
        channel = _integer(frequency.get('earfcn'), maximum=262143)
        for item in _items(frequency.get('cells')):
            add_rat('lte')
            neighbors.append(neighbor(item, channel, 'inter'))
    return dict(rats=rats, cells=cells, bands=bands, bandwidths=bandwidths, neighbors=neighbors)
