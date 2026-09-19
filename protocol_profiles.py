"""模组协议组、完整帧模板校验与渲染。"""
from copy import deepcopy
from dataclasses import dataclass
import re
import uuid


PROFILE_DSM = 'dsm'
PROFILE_KDS = 'kds'
MODALITIES = ('face', 'palm')


@dataclass(frozen=True)
class FunctionSpec:
    label: str
    required_placeholders: tuple = ()


FUNCTION_SPECS = {
    'register': FunctionSpec('注册'),
    'recognize': FunctionSpec('识别'),
    'get_version': FunctionSpec('获取版本号'),
    'get_all_user_ids': FunctionSpec('获取所有用户ID'),
    'delete_user': FunctionSpec('删除指定用户ID', ('USER_ID',)),
    'delete_all': FunctionSpec('删除所有用户'),
    'restart': FunctionSpec('重启模组'),
    'standby': FunctionSpec('待机'),
    'enter_demo': FunctionSpec('进入演示'),
    'exit_demo': FunctionSpec('退出演示'),
    'enter_debug': FunctionSpec('进入Debug'),
    'exit_debug': FunctionSpec('退出Debug'),
    'set_baudrate': FunctionSpec('设置波特率', ('BAUD_CODE',)),
    'get_jpeg_size': FunctionSpec('获取JPEG大小'),
    'get_raw_size': FunctionSpec('获取RAW大小'),
    'image_upload': FunctionSpec('下载图片分段', ('OFFSET', 'SIZE')),
    'ota_enter': FunctionSpec('进入OTA'),
    'ota_header': FunctionSpec(
        '发送OTA头', ('FILE_SIZE', 'PACKET_COUNT', 'PACKET_SIZE', 'MD5_ASCII')
    ),
    'ota_packet': FunctionSpec(
        '发送OTA数据包', ('PACKET_INDEX', 'PACKET_LENGTH', 'FIRMWARE_DATA')
    ),
}

PLACEHOLDER_WIDTHS = {
    'DATA_SIZE': 2,
    'USER_ID': 2,
    'BAUD_CODE': 1,
    'OFFSET': 4,
    'SIZE': 4,
    'FILE_SIZE': 4,
    'PACKET_COUNT': 4,
    'PACKET_SIZE': 2,
    'MD5_ASCII': 32,
    'PACKET_INDEX': 2,
    'PACKET_LENGTH': 2,
    'FIRMWARE_DATA': None,
    'CHECKSUM': None,
}

_PLACEHOLDER_RE = re.compile(r'^\{([A-Za-z_][A-Za-z0-9_]*)(?::(\d+))?\}$')
_HEX_BYTE_RE = re.compile(r'^[0-9A-Fa-f]{2}$')


class ProtocolTemplateError(ValueError):
    """完整帧或帧模板不符合固定线协议。"""


def build_packet(msg_id, data=b''):
    """按 EF AA + MID + 大端长度 + data + XOR 构建完整帧。"""
    if not 0 <= int(msg_id) <= 0xFF:
        raise ProtocolTemplateError('消息ID必须在00到FF之间')
    data = bytes(data)
    if len(data) > 0xFFFF:
        raise ProtocolTemplateError('data长度超过65535字节')
    size = len(data).to_bytes(2, 'big')
    checksum = int(msg_id) ^ size[0] ^ size[1]
    for value in data:
        checksum ^= value
    return b'\xEF\xAA' + bytes([int(msg_id)]) + size + data + bytes([checksum])


def packet_to_text(packet):
    return ' '.join(f'{value:02X}' for value in packet)


def _static_frame(msg_id, data=b''):
    return packet_to_text(build_packet(msg_id, data))


def _dynamic_frame(msg_id, *data_tokens):
    return ' '.join((
        'EF', 'AA', f'{msg_id:02X}', '{DATA_SIZE:2}', *data_tokens, '{CHECKSUM}'
    ))


_REGISTER_DATA = b'\x00tester' + b'\x00' * 27 + b'\x05'
_RECOGNIZE_DATA = b'\x00\x05'

_COMMON_DEFAULTS = {
    'get_version': _static_frame(0x30),
    'restart': _static_frame(0x55),
    'standby': _static_frame(0x10),
    'enter_demo': _static_frame(0xFE, b'\x01'),
    'exit_demo': _static_frame(0xFE, b'\x00'),
    'enter_debug': _static_frame(0xF0, b'\x01'),
    'exit_debug': _static_frame(0xF0, b'\x00'),
    'set_baudrate': _dynamic_frame(0x51, '{BAUD_CODE:1}'),
    'get_jpeg_size': _static_frame(0x14),
    'get_raw_size': _static_frame(0x15),
    'image_upload': _dynamic_frame(0x18, '{OFFSET:4}', '{SIZE:4}'),
    'ota_enter': _static_frame(0x40),
    'ota_header': _dynamic_frame(
        0x43, '{FILE_SIZE:4}', '{PACKET_COUNT:4}', '{PACKET_SIZE:2}',
        '{MD5_ASCII:32}'
    ),
    'ota_packet': _dynamic_frame(
        0x44, '{PACKET_INDEX:2}', '{PACKET_LENGTH:2}', '{FIRMWARE_DATA}'
    ),
}

DSM_DEFAULTS = {
    'face': {
        **_COMMON_DEFAULTS,
        'register': _static_frame(0x1D, _REGISTER_DATA),
        'recognize': _static_frame(0x12, _RECOGNIZE_DATA),
        'get_all_user_ids': _static_frame(0x24),
        'delete_user': _dynamic_frame(0x20, '{USER_ID:2}'),
        'delete_all': _static_frame(0x21),
    },
    'palm': {
        **_COMMON_DEFAULTS,
        'register': _static_frame(0x62, _REGISTER_DATA),
        'recognize': _static_frame(0x63, _RECOGNIZE_DATA),
        'get_all_user_ids': _static_frame(0x64),
        'delete_user': _dynamic_frame(0x65, '{USER_ID:2}'),
        'delete_all': _static_frame(0x66),
    },
}

KDS_OVERRIDES = {
    'face': {},
    'palm': {
        'register': _static_frame(0x80, _REGISTER_DATA),
        'recognize': _static_frame(0x81, _RECOGNIZE_DATA),
        'delete_all': _static_frame(0x82),
        'delete_user': _dynamic_frame(0x83, '{USER_ID:2}'),
        'get_all_user_ids': _static_frame(0x84),
    },
}

BUILTIN_PROFILE_NAMES = {
    PROFILE_DSM: 'DSM',
    PROFILE_KDS: 'KDS',
}


def _parse_token(token):
    match = _PLACEHOLDER_RE.match(token)
    if not match:
        if not _HEX_BYTE_RE.match(token):
            raise ProtocolTemplateError(
                f'“{token}”不是两位十六进制字节或合法占位符'
            )
        return ('byte', token.upper(), None)

    name = match.group(1).upper()
    if name not in PLACEHOLDER_WIDTHS:
        raise ProtocolTemplateError(f'未知占位符 {{{name}}}')
    supplied_width = int(match.group(2)) if match.group(2) else None
    expected_width = PLACEHOLDER_WIDTHS[name]
    if expected_width is None:
        if supplied_width is not None:
            raise ProtocolTemplateError(f'占位符 {{{name}}}不应指定宽度')
    elif supplied_width != expected_width:
        raise ProtocolTemplateError(
            f'占位符 {{{name}}}必须写为 {{{name}:{expected_width}}}'
        )
    canonical = f'{{{name}:{expected_width}}}' if expected_width is not None else f'{{{name}}}'
    return ('placeholder', canonical, name)


def normalize_template(template, function_key):
    """校验并规范化某项完整静态帧或动态帧模板。"""
    if function_key not in FUNCTION_SPECS:
        raise ProtocolTemplateError(f'未知功能：{function_key}')
    if not isinstance(template, str) or not template.strip():
        raise ProtocolTemplateError('指令不能为空')

    raw_tokens = template.strip().split()
    parsed = [_parse_token(token) for token in raw_tokens]
    tokens = [item[1] for item in parsed]
    placeholders = [item[2] for item in parsed if item[0] == 'placeholder']

    if len(tokens) < 6:
        raise ProtocolTemplateError('完整帧至少应包含同步头、MID、长度和校验')
    if tokens[:2] != ['EF', 'AA']:
        raise ProtocolTemplateError('同步头必须为 EF AA')
    if parsed[2][0] != 'byte':
        raise ProtocolTemplateError('msgID必须是两位十六进制字节')

    required = set(FUNCTION_SPECS[function_key].required_placeholders)
    dynamic = bool(placeholders)
    if dynamic:
        if tokens[3] != '{DATA_SIZE:2}':
            raise ProtocolTemplateError('动态模板的dataSize必须写为 {DATA_SIZE:2}')
        if tokens[-1] != '{CHECKSUM}':
            raise ProtocolTemplateError('动态模板必须以 {CHECKSUM} 结尾')
        if placeholders.count('DATA_SIZE') != 1 or placeholders.count('CHECKSUM') != 1:
            raise ProtocolTemplateError('DATA_SIZE和CHECKSUM各只能出现一次')
        data_placeholders = set(placeholders) - {'DATA_SIZE', 'CHECKSUM'}
        allowed = required
        unexpected = data_placeholders - allowed
        missing = required - data_placeholders
        if unexpected:
            names = '、'.join(sorted(unexpected))
            raise ProtocolTemplateError(f'此功能不允许占位符：{names}')
        if missing:
            names = '、'.join(sorted(missing))
            raise ProtocolTemplateError(f'缺少动态占位符：{names}')
        for name in required:
            if placeholders.count(name) != 1:
                raise ProtocolTemplateError(f'占位符 {{{name}}}必须且只能出现一次')
    else:
        if required:
            names = '、'.join(sorted(required))
            raise ProtocolTemplateError(f'此功能必须使用动态占位符：{names}')
        try:
            packet = bytes.fromhex(' '.join(tokens))
        except ValueError as exc:
            raise ProtocolTemplateError('完整帧包含无效十六进制') from exc
        declared_size = int.from_bytes(packet[3:5], 'big')
        actual_size = len(packet) - 6
        if declared_size != actual_size:
            raise ProtocolTemplateError(
                f'dataSize为{declared_size}，但data实际为{actual_size}字节'
            )
        checksum = 0
        for value in packet[2:]:
            checksum ^= value
        if checksum:
            raise ProtocolTemplateError('校验字节不符合现有XOR规则')

    return ' '.join(tokens)


def _runtime_bytes(name, value):
    width = PLACEHOLDER_WIDTHS[name]
    if name == 'FIRMWARE_DATA':
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ProtocolTemplateError('FIRMWARE_DATA必须是字节数据')
        return bytes(value)
    if name == 'MD5_ASCII':
        if isinstance(value, str):
            value = value.encode('ascii')
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ProtocolTemplateError('MD5_ASCII必须是32字节ASCII数据')
        result = bytes(value)
        if len(result) != width:
            raise ProtocolTemplateError(f'MD5_ASCII必须正好是{width}字节')
        return result
    if isinstance(value, int):
        if value < 0 or value >= 1 << (8 * width):
            raise ProtocolTemplateError(f'{name}超出{width}字节无符号整数范围')
        return value.to_bytes(width, 'big')
    if isinstance(value, (bytes, bytearray, memoryview)):
        result = bytes(value)
        if len(result) != width:
            raise ProtocolTemplateError(f'{name}必须正好是{width}字节')
        return result
    raise ProtocolTemplateError(f'{name}必须是整数或{width}字节数据')


def render_template(template, function_key, runtime_values=None):
    """将完整帧/模板渲染为可直接写串口的字节串。"""
    normalized = normalize_template(template, function_key)
    tokens = normalized.split()
    if not any(token.startswith('{') for token in tokens):
        return bytes.fromhex(normalized)

    values = {str(key).upper(): value for key, value in (runtime_values or {}).items()}
    data = bytearray()
    for token in tokens[4:-1]:
        parsed = _parse_token(token)
        if parsed[0] == 'byte':
            data.append(int(parsed[1], 16))
            continue
        name = parsed[2]
        if name not in values:
            raise ProtocolTemplateError(f'缺少运行时数据：{name}')
        data.extend(_runtime_bytes(name, values[name]))

    return build_packet(int(tokens[2], 16), data)


def profile_exists(profile_id, custom_profiles):
    if profile_id in BUILTIN_PROFILE_NAMES:
        return True
    return any(profile.get('id') == profile_id for profile in custom_profiles)


def profile_name(profile_id, custom_profiles):
    if profile_id in BUILTIN_PROFILE_NAMES:
        return BUILTIN_PROFILE_NAMES[profile_id]
    for profile in custom_profiles:
        if profile.get('id') == profile_id:
            return profile.get('name', profile_id)
    return BUILTIN_PROFILE_NAMES[PROFILE_DSM]


def effective_templates(profile_id, modality, custom_profiles):
    """返回协议组某模态下继承完成后的全部模板。"""
    if modality not in MODALITIES:
        raise KeyError(f'未知模态：{modality}')
    result = dict(DSM_DEFAULTS[modality])
    if profile_id == PROFILE_KDS:
        result.update(KDS_OVERRIDES[modality])
        return result
    if profile_id == PROFILE_DSM:
        return result
    for profile in custom_profiles:
        if profile.get('id') == profile_id:
            result.update(profile.get('overrides', {}).get(modality, {}))
            return result
    return result


def resolve_packet(profile_id, modality, function_key, custom_profiles, runtime_values=None):
    templates = effective_templates(profile_id, modality, custom_profiles)
    if function_key not in templates:
        raise ProtocolTemplateError(f'协议组缺少“{FUNCTION_SPECS[function_key].label}”指令')
    packet = render_template(templates[function_key], function_key, runtime_values)
    return packet, packet[2]


def response_variant(profile_id, modality):
    """自定义组默认沿用DSM响应解析；仅内置KDS手掌使用KDS变体。"""
    return 'kds' if profile_id == PROFILE_KDS and modality == 'palm' else 'dsm'


def canonical_response_mid(function_key, profile_id, modality):
    """返回既有响应解析器使用的语义MID。"""
    common = {
        'get_version': 0x30,
        'restart': 0x55,
        'standby': 0x10,
        'enter_demo': 0xFE,
        'exit_demo': 0xFE,
        'enter_debug': 0xF0,
        'exit_debug': 0xF0,
        'set_baudrate': 0x51,
        'get_jpeg_size': 0x14,
        'get_raw_size': 0x15,
        'image_upload': 0x18,
        'ota_enter': 0x40,
        'ota_header': 0x43,
        'ota_packet': 0x44,
    }
    if function_key in common:
        return common[function_key]
    if modality == 'face':
        return {
            'register': 0x1D,
            'recognize': 0x12,
            'get_all_user_ids': 0x24,
            'delete_user': 0x20,
            'delete_all': 0x21,
        }[function_key]
    if response_variant(profile_id, modality) == 'kds':
        return {
            'register': 0x80,
            'recognize': 0x81,
            'delete_all': 0x82,
            'delete_user': 0x83,
            'get_all_user_ids': 0x84,
        }[function_key]
    return {
        'register': 0x62,
        'recognize': 0x63,
        'get_all_user_ids': 0x64,
        'delete_user': 0x65,
        'delete_all': 0x66,
    }[function_key]


def sanitize_custom_profiles(raw_profiles):
    """清洗持久化配置；损坏覆盖被丢弃并回退DSM。"""
    clean = []
    errors = []
    used_ids = set(BUILTIN_PROFILE_NAMES)
    used_names = {name.casefold() for name in BUILTIN_PROFILE_NAMES.values()}
    if not isinstance(raw_profiles, list):
        return clean, ['protocol_profiles不是列表，已回退DSM'] if raw_profiles else []

    for index, raw in enumerate(raw_profiles):
        if not isinstance(raw, dict):
            errors.append(f'第{index + 1}个协议组不是对象，已忽略')
            continue
        profile_id = str(raw.get('id', '')).strip()
        name = str(raw.get('name', '')).strip()
        if not profile_id or profile_id in used_ids or not name or name.casefold() in used_names:
            errors.append(f'第{index + 1}个协议组ID或名称无效/重复，已忽略')
            continue
        overrides = {'face': {}, 'palm': {}}
        raw_overrides = raw.get('overrides', {})
        if not isinstance(raw_overrides, dict):
            raw_overrides = {}
            errors.append(f'协议组“{name}”的覆盖格式无效，已回退DSM')
        for modality in MODALITIES:
            values = raw_overrides.get(modality, {})
            if not isinstance(values, dict):
                errors.append(f'协议组“{name}”的{modality}覆盖无效，已回退DSM')
                continue
            for function_key, template in values.items():
                if function_key not in FUNCTION_SPECS or not isinstance(template, str):
                    errors.append(f'协议组“{name}”包含未知/无效功能“{function_key}”，已忽略')
                    continue
                try:
                    overrides[modality][function_key] = normalize_template(template, function_key)
                except ProtocolTemplateError as exc:
                    errors.append(
                        f'协议组“{name}”{modality}/{function_key}无效（{exc}），已回退DSM'
                    )
        clean.append({'id': profile_id, 'name': name, 'overrides': overrides})
        used_ids.add(profile_id)
        used_names.add(name.casefold())
    return clean, errors


def new_profile(name='新协议组', source_profile_id=PROFILE_DSM, custom_profiles=None):
    """创建继承DSM的稀疏自定义组；复制时仅保存相对DSM的差异。"""
    custom_profiles = custom_profiles or []
    overrides = {'face': {}, 'palm': {}}
    for modality in MODALITIES:
        source = effective_templates(source_profile_id, modality, custom_profiles)
        for function_key, template in source.items():
            if template != DSM_DEFAULTS[modality][function_key]:
                overrides[modality][function_key] = template
    return {
        'id': f'custom-{uuid.uuid4().hex}',
        'name': name,
        'overrides': overrides,
    }


def clone_profiles(profiles):
    return deepcopy(profiles)
