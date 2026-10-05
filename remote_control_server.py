"""局域网手机遥控服务：仅提供受控业务动作，不暴露原始串口命令。"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import mimetypes
import secrets
import socket
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse


REMOTE_ACTIONS = {
    'face_register': '人脸注册',
    'face_recognize': '人脸识别',
    'palm_register': '手掌注册',
    'palm_recognize': '手掌识别',
    'get_version': '获取版本号',
    'get_users': '获取所有用户ID',
    'download_jpeg': '下载JPEG',
    'download_raw': '下载RAW',
}
MAX_BODY_BYTES = 16 * 1024
SESSION_TTL_SECONDS = 8 * 60 * 60
ACTION_COOLDOWN_SECONDS = 0.6
# 有界重放窗口，覆盖一次1000次重复测试；不保证无限断网后的事件交付。
RESULT_EVENT_CAPACITY = 1024
NOTE_EVENT_CAPACITY = 32
_LOG = logging.getLogger(__name__)
_UNSPECIFIED_ORIGIN = object()  # Legacy callers omit provenance; explicit None is no stream.


@dataclass
class RemoteSession:
    token: str
    created_at: float
    last_seen: float


@dataclass(frozen=True)
class RemoteOrigin:
    stream_id: str
    session_token: Optional[str] = None


class RemoteActionRequest:
    """Atomic queue acceptance, not physical cancellation of claimed serial work."""

    def __init__(self, server=None, origin=None):
        self.server = server
        self.origin = origin
        self._lock = threading.Lock()
        self.done = threading.Event()
        self.state = 'queued'
        self.result = None

    def claim(self):
        if self.server is not None:
            return self.server.claim_request(self)
        with self._lock:
            return self._claim_locked()

    def _claim_locked(self):
        if self.state != 'queued':
            return False
        self.state = 'claimed'
        return True

    def cancel(self, result):
        """Only queued work can be cancelled; claimed wire work is irrevocable."""
        with self._lock:
            if self.state != 'queued':
                return False
            self.result = dict(result)
            self.state = 'cancelled'
            self.done.set()
            return True

    def complete(self, result):
        with self._lock:
            # Late callbacks cannot resurrect cancellation or rewrite a final result.
            if self.state != 'claimed':
                return False
            self.result = dict(result)
            self.state = 'completed'
            self.done.set()
        if self.server is not None and hasattr(self, 'action_name'):
            self.server._record_action_response(self, self.result)
        return True

    def wait(self, timeout):
        self.done.wait(timeout)
        with self._lock:
            if self.state == 'queued':
                self.state = 'cancelled'
                self.result = {'ok': False, 'state': 'cancelled',
                               'message': '主界面忙，排队操作已取消，未执行'}
                self.done.set()
            if self.state == 'claimed':
                return {'ok': True, 'state': 'in_progress',
                        'message': '操作已开始，结果尚未确定，请勿重试'}
            return dict(self.result)

    def pending(self):
        with self._lock:
            return self.state in ('queued', 'claimed')


class RemoteActionArgument(str):
    """String-compatible second callback argument, including custom JSON."""

    def __new__(cls, value, request):
        argument = super().__new__(cls, value)
        argument.request = request
        return argument


class RemoteControlServer:
    """线程化HTTP服务器，HTTP线程只发出动作回调，不触碰Qt对象。"""

    def __init__(self, action_callback: Callable[[str, str], object],
                 status_provider: Optional[Callable[[], dict]] = None,
                 host='0.0.0.0', port=0, *, queued_action_callback=False):
        self.action_callback = action_callback
        # Legacy callbacks execute synchronously; queue adapters claim at dequeue.
        self.queued_action_callback = queued_action_callback
        self.status_provider = status_provider or (lambda: {})
        self._status_snapshot = None  # None保留旧provider API；显式发布后只读缓存
        self.host = host
        self.port = port
        self.pair_code = ''
        self._session: Optional[RemoteSession] = None
        self._lock = threading.RLock()
        self._last_action_at = {}
        self._server = None
        self._thread = None
        self._started = False
        self._result_stream_id = None
        self._latest_result_id = 0
        self._result_events = deque(maxlen=RESULT_EVENT_CAPACITY)
        self._latest_note_id = 0
        self._note_events = {}  # {(nid, status): event_dict}
        self._operation_sequence = 0  # 每次新操作递增
        self._status_changed = threading.Condition(self._lock)
        self._status_revision = 0  # 状态游标，与操作序列号无关；仅新stream启动时重置
        self.response_text = ''
        self.preview = {'version': 0, 'display_name': '', 'items': []}
        self._preview_files = {}
        self.last_result = {
            'ok': True,
            'message': '服务尚未启动',
            'action': None,
            'updated_at': None,
        }

    @property
    def started(self):
        return self._started and self._server is not None

    @property
    def actual_port(self):
        return self._server.server_address[1] if self._server else None

    def start(self):
        if self.started:
            return self.connection_info()
        self.pair_code = f'{secrets.randbelow(1_000_000):06d}'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send(self, status, payload, content_type='application/json; charset=utf-8'):
                if isinstance(payload, bytes):
                    data = payload
                elif isinstance(payload, str):
                    data = payload.encode('utf-8')
                else:
                    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise ValueError('请求长度无效')
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError('请求内容为空或过大')
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError('请求必须是UTF-8 JSON') from exc
                if not isinstance(value, dict):
                    raise ValueError('请求JSON必须是对象')
                return value

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers',
                                 'Content-Type, X-Remote-Token, X-Wait-Timeout, '
                                 'X-Last-Seen-Revision, X-Status-Stream, X-Last-Seen-Sequence')
                self.end_headers()

            def do_GET(self):
                path = urlparse(self.path).path
                if path == '/':
                    self._send(200, REMOTE_PAGE, 'text/html; charset=utf-8')
                elif path == '/api/status':
                    self._send(200, owner.status_payload(self.headers.get('X-Remote-Token')))
                elif path == '/api/wait':
                    # 旧操作序号不是状态游标；缺少revision的旧客户端立即获得快照。
                    try:
                        payload = owner.wait_for_change(
                            self.headers.get('X-Remote-Token'),
                            self.headers.get('X-Wait-Timeout', '25'),
                            self.headers.get('X-Last-Seen-Revision'),
                            self.headers.get('X-Status-Stream'),
                        )
                    except (ValueError, TypeError) as exc:
                        self._send(400, {'ok': False, 'message': str(exc)})
                        return
                    self._send(200, payload)
                elif path.startswith('/api/preview/'):
                    item_id = path.rsplit('/', 1)[-1]
                    result = owner.preview_file(self.headers.get('X-Remote-Token'), item_id)
                    if result is None:
                        self._send(404, {'ok': False, 'message': '预览不存在或未授权'})
                    else:
                        mime, data = result
                        self._send(200, data, mime)
                else:
                    self._send(404, {'ok': False, 'message': '路径不存在'})

            def do_POST(self):
                path = urlparse(self.path).path
                try:
                    body = self._read_json()
                except ValueError as exc:
                    self._send(400, {'ok': False, 'message': str(exc)})
                    return
                if path == '/api/pair':
                    self._send(200, owner.pair(str(body.get('code', ''))))
                elif path == '/api/unpair':
                    self._send(200, owner.unpair(self.headers.get('X-Remote-Token')))
                elif path == '/api/action':
                    self._send(200, owner.action(
                        self.headers.get('X-Remote-Token'), str(body.get('action', ''))
                    ))
                elif path == '/api/custom-action':
                    self._send(200, owner.custom_action(
                        self.headers.get('X-Remote-Token'),
                        str(body.get('name', '')), str(body.get('hex', ''))
                    ))
                else:
                    self._send(404, {'ok': False, 'message': '路径不存在'})

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError:
            self._server = None
            raise
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name='capLG-remote-http',
            daemon=True,
        )
        with self._lock:
            self._result_stream_id = secrets.token_urlsafe(18)
            self._latest_result_id = 0
            self._result_events.clear()
            self._latest_note_id = 0
            self._note_events.clear()
            self._operation_sequence = 0
            self._status_revision = 0
            self._started = True
            self._notify_status_changed_locked()
        self._thread.start()
        return self.connection_info()

    def stop(self):
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            self._started = False
            self._result_stream_id = None
            self._latest_result_id = 0
            self._result_events.clear()
            self._latest_note_id = 0
            self._note_events.clear()
            self._session = None
            self.pair_code = ''
            self._last_action_at.clear()
            self.preview = {'version': 0, 'display_name': '', 'items': []}
            self._preview_files.clear()
            self.last_result = {
                'ok': True, 'message': '远程控制已停止',
                'action': None, 'updated_at': _now_text(),
            }
            self._notify_status_changed_locked()
        if server:
            server.shutdown()
            server.server_close()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def capture_origin(self):
        """Local submissions also remember a stopped/no-server stream as None."""
        with self._lock:
            return RemoteOrigin(self._result_stream_id) if self.started else None

    def _origin_valid_locked(self, origin):
        return (origin is not None and self.started
                and self._result_stream_id == origin.stream_id
                and (origin.session_token is None
                     or self._authorized(origin.session_token)))

    def claim_request(self, request):
        # Validation and queued->claimed are one acceptance point. Never keep
        # this lock while executing Qt callbacks or writing/flushing serial.
        with self._lock, request._lock:
            if request.state != 'queued':
                return False
            if not self._origin_valid_locked(request.origin):
                request.state = 'cancelled'
                request.result = {'ok': False, 'state': 'cancelled',
                                  'message': '原远程会话已失效，操作未执行'}
                request.done.set()
                return False
            return request._claim_locked()

    def origin_valid(self, origin):
        with self._lock:
            return self._origin_valid_locked(origin)

    def publish_result(self, operation, modality, success, *, origin=_UNSPECIFIED_ORIGIN):
        """发布注册/识别的最终结论，不携带模组回复或用户身份信息。"""
        with self._lock:
            if not self.started or (origin is not _UNSPECIFIED_ORIGIN
                                    and not self._origin_valid_locked(origin)):
                return
            if operation not in ('register', 'recognize'):
                raise ValueError('结果操作必须是register或recognize')
            if modality not in ('face', 'palm'):
                raise ValueError('结果类型必须是face或palm')
            if not isinstance(success, bool):
                raise TypeError('结果success必须是bool')
            self._latest_result_id += 1
            self._result_events.append({
                'id': self._latest_result_id,
                'operation': operation,
                'modality': modality,
                'success': success,
                'message': ('人脸' if modality == 'face' else '手掌')
                           + ('注册' if operation == 'register' else '识别')
                           + ('成功' if success else '失败'),
            })
            self._notify_status_changed_locked()
            _LOG.debug('Remote result id=%s operation=%s modality=%s success=%s',
                       self._latest_result_id, operation, modality, success)

    def publish_note(self, nid, status, message, *, origin=_UNSPECIFIED_ORIGIN):
        """发布 NOTE 状态消息，相同 (nid, status) 只保留最新的一条。"""
        with self._lock:
            if not self.started or (origin is not _UNSPECIFIED_ORIGIN
                                    and not self._origin_valid_locked(origin)):
                return
            if not isinstance(nid, int) or not isinstance(status, int):
                raise TypeError('nid 和 status 必须是整数')
            if not isinstance(message, str):
                raise TypeError('message 必须是字符串')

            # 使用 (nid, status) 作为去重键
            key = (nid, status)
            self._latest_note_id += 1
            self._note_events[key] = {
                'id': self._latest_note_id,
                'nid': nid,
                'status': status,
                'message': message,
                'operation_sequence': self._operation_sequence,
            }

            # 限制容量：保留最新的 NOTE_EVENT_CAPACITY 条
            if len(self._note_events) > NOTE_EVENT_CAPACITY:
                # 按 id 排序，删除最旧的
                oldest_key = min(self._note_events.keys(),
                                key=lambda k: self._note_events[k]['id'])
                del self._note_events[oldest_key]

            self._notify_status_changed_locked()
            _LOG.debug('remote note id=%d nid=%d status=%d operation_sequence=%d',
                       self._latest_note_id, nid, status, self._operation_sequence)

    def new_operation(self, *, origin=_UNSPECIFIED_ORIGIN):
        """标记新操作开始，递增序列号并清空 NOTE"""
        with self._lock:
            if not self.started or (origin is not _UNSPECIFIED_ORIGIN
                                    and not self._origin_valid_locked(origin)):
                return
            self._operation_sequence += 1
            self._note_events.clear()
            self._notify_status_changed_locked()
            _LOG.debug('remote operation_sequence=%d', self._operation_sequence)

    def set_status_snapshot(self, status):
        """业务线程主动发布快照；缓存及revision在同一锁内更新。

        第一次调用切换为缓存模式，此后HTTP线程不再调用status_provider。
        调用方及响应接收方均不能修改内部快照。
        """
        snapshot = deepcopy(dict(status or {}))
        with self._lock:
            if self._status_snapshot != snapshot:
                self._status_snapshot = snapshot
                self._notify_status_changed_locked()

    def set_response_text(self, text):
        with self._lock:
            text = str(text or '')[-20000:]
            if self.response_text != text:
                self.response_text = text
                self._notify_status_changed_locked()

    def set_preview_items(self, items, display_name=''):
        metadata = []
        files = {}
        with self._lock:
            version = int(self.preview.get('version', 0)) + 1
            for index, item in enumerate(items[:10]):
                item_id = f'{version}-{index}'
                entry = {
                    'id': item_id,
                    'name': str(item.get('name', '')),
                    'size': int(item.get('size', 0)),
                    'mime': str(item.get('mime', 'application/octet-stream')),
                    'previewable': bool(item.get('previewable', False)),
                    'url': f'/api/preview/{item_id}' if item.get('previewable') else None,
                }
                metadata.append(entry)
                if entry['previewable'] and item.get('data'):
                    files[item_id] = (entry['mime'], bytes(item['data']))
            self.preview = {
                'version': version,
                'display_name': str(display_name or ''),
                'items': metadata,
            }
            self._preview_files = files
            self._notify_status_changed_locked()

    def clear_preview(self):
        self.set_preview_items([])

    def preview_file(self, token, item_id):
        if not self._authorized(token):
            return None
        with self._lock:
            return self._preview_files.get(item_id)

    def connection_info(self):
        addresses = local_ipv4_addresses()
        urls = [f'http://{address}:{self.actual_port}' for address in addresses]
        return {
            'started': self.started,
            'port': self.actual_port,
            'pair_code': self.pair_code if self.started else None,
            'addresses': addresses,
            'urls': urls,
            'auto_urls': [f'{url}/?code={self.pair_code}' for url in urls]
                        if self.started else [],
        }

    def _notify_status_changed_locked(self):
        """调用者持有共享RLock：版本递增与广播是一次原子状态发布。"""
        self._status_revision += 1
        self._status_changed.notify_all()

    def pair(self, code):
        with self._lock:
            if not self.started:
                return {'ok': False, 'message': '远程服务未启动'}
            if not secrets.compare_digest(code, self.pair_code):
                return {'ok': False, 'message': '配对码错误'}
            now = time.monotonic()
            token = secrets.token_urlsafe(24)
            self._session = RemoteSession(token, now, now)
            self._notify_status_changed_locked()
        return {'ok': True, 'token': token, 'message': '配对成功'}

    def _authorized(self, token):
        with self._lock:
            now = time.monotonic()
            session = self._session
            if session and now - session.last_seen > SESSION_TTL_SECONDS:
                self._session = None
                self._notify_status_changed_locked()
                return False
            if not session or not token or not secrets.compare_digest(token, session.token):
                return False
            session.last_seen = now
            return True

    def status_payload(self, token=None):
        # 旧provider兼容路径不能持有服务器锁；动态业务应主动发布缓存。
        with self._lock:
            use_provider = self._status_snapshot is None
        legacy_status = dict(self.status_provider() or {}) if use_provider else {}
        with self._lock:
            # 再次检查：provider运行期间可能已经切换为缓存模式。
            status = (deepcopy(self._status_snapshot) if self._status_snapshot is not None
                      else legacy_status)
            paired = self._authorized(token)
            result_feed = None
            if paired and self.started:
                note_events_list = sorted(self._note_events.values(), key=lambda e: e['id'])
                result_feed = {
                    'version': 1,
                    'stream_id': self._result_stream_id,
                    'operation_sequence': self._operation_sequence,
                    'oldest_id': (self._result_events[0]['id'] if self._result_events
                                  else self._latest_result_id + 1),
                    'latest_id': self._latest_result_id,
                    'events': [dict(event) for event in self._result_events],
                    'note_events': [dict(event) for event in note_events_list],
                }
            status.update({
                'ok': True,
                'api_version': 2,
                'supports_custom_action': True,
                'supports_result_events': True,
                'supports_long_poll': True,
                'status_revision': self._status_revision,
                'status_stream_id': self._result_stream_id,
                'result_feed': result_feed,
                'started': self.started,
                'paired': paired,
                'actions': dict(REMOTE_ACTIONS),
                'response_text': self.response_text,
                'preview': {
                    **self.preview,
                    'items': [dict(item) for item in self.preview['items']],
                },
                'last_result': dict(self.last_result),
            })
        return status

    def wait_for_change(self, token=None, timeout=25, last_seen_revision=None,
                        status_stream_id=None):
        """按stream+revision等待，兼容没有状态游标的客户端（立即返回快照）。"""
        try:
            timeout = float(timeout)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('等待超时必须是0到30秒的有限数值') from exc
        if not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError('等待超时必须是0到30秒的有限数值')
        if last_seen_revision is not None:
            if isinstance(last_seen_revision, str):
                if (not last_seen_revision
                        or any(char not in '0123456789' for char in last_seen_revision)):
                    raise ValueError('状态revision必须是非负整数')
                last_seen_revision = int(last_seen_revision)
            elif type(last_seen_revision) is not int or last_seen_revision < 0:
                raise ValueError('状态revision必须是非负整数')

        deadline = time.monotonic() + timeout
        with self._status_changed:
            if not self._authorized(token):
                return {'ok': False, 'message': '请先配对'}
            # 谓词检查与Condition.wait共享RLock，避免检查和等待之间丢失通知。
            # revision使用不等而非大于：过期/未来游标也必须立即重新同步。
            while (last_seen_revision is not None and self.started
                   and self._session is not None and self._session.token == token
                   and self._status_revision == last_seen_revision
                   and self._result_stream_id == status_stream_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._status_changed.wait(remaining)
        # 缓存业务状态与revision同锁快照；旧provider仍仅在锁外调用。
        return self.status_payload(token)

    def unpair(self, token):
        with self._lock:
            if not self._authorized(token):
                return {'ok': False, 'message': '未授权'}
            self._session = None
            self._notify_status_changed_locked()
        return {'ok': True, 'message': '已解除配对'}

    def custom_action(self, token, name, hex_text):
        with self._lock:
            if not self._authorized(token):
                return {'ok': False, 'message': '请先配对'}
            origin = RemoteOrigin(self._result_stream_id, token)
        name = name.strip()
        compact = ''.join(hex_text.replace(',', ' ').replace('-', ' ').split()).upper()
        if not name or len(name) > 32:
            return {'ok': False, 'message': '命令名称不能为空且不能超过32字符'}
        if (not compact or len(compact) % 2
                or any(char not in '0123456789ABCDEF' for char in compact)):
            return {'ok': False, 'message': '请输入有效的偶数字节十六进制命令'}
        if len(compact) > 4096 or not compact.startswith('EFAA') or len(compact) < 12:
            return {'ok': False, 'message': '完整帧必须以EF AA开头，长度为6到2048字节'}
        return self._execute_action('__custom__', json.dumps({
            'name': name, 'hex': compact
        }, ensure_ascii=False), origin)

    def action(self, token, action_name):
        with self._lock:
            if not self._authorized(token):
                return {'ok': False, 'message': '请先配对'}
            if action_name not in REMOTE_ACTIONS:
                return {'ok': False, 'message': '不支持的远程操作'}
            now = time.monotonic()
            previous = self._last_action_at.get(action_name, 0)
            if now - previous < ACTION_COOLDOWN_SECONDS:
                return {'ok': False, 'message': '操作过于频繁，请稍后再试'}
            self._last_action_at[action_name] = now
            origin = RemoteOrigin(self._result_stream_id, token)
        return self._execute_action(action_name, token, origin)

    def _record_action_response(self, request, response):
        with self._lock:
            if self._origin_valid_locked(request.origin):
                # Terminal state is authoritative, even if a delayed callback returns
                # an earlier in_progress snapshot (or tries to resurrect cancellation).
                with request._lock:
                    if request.state in ('completed', 'cancelled'):
                        response = request.result
                response = dict(response)
                response.setdefault('action', request.action_name)
                response.setdefault('updated_at', _now_text())
                if self.last_result != response:
                    self.last_result = response
                    self._notify_status_changed_locked()

    def _execute_action(self, action_name, argument, origin):
        request = RemoteActionRequest(self, origin)
        request.action_name = action_name
        # String-compatible metadata preserves the two-argument callback and custom JSON.
        argument = RemoteActionArgument(argument, request)
        if self.queued_action_callback:
            # Recheck before dispatch too: authentication may predate a restart.
            # The adapter must still claim atomically when its queue is consumed.
            if not self.origin_valid(origin):
                request.cancel({'ok': False, 'state': 'cancelled',
                                'message': '原远程会话已失效，操作未执行'})
        else:
            # Compatibility callbacks have no dequeue stage: claim before invoking
            # them, without holding the server lock across their side effects.
            request.claim()
        if request.pending():
            try:
                result = self.action_callback(action_name, argument)
                if isinstance(result, dict):
                    response = dict(result)
                else:
                    response = {'ok': bool(result),
                                'message': '操作已发送' if result else '操作被拒绝'}
            except Exception as exc:
                response = {'ok': False, 'message': f'执行失败：{exc}'}
            if self.queued_action_callback:
                # An adapter returning before dequeue cannot leave executable work
                # behind a rejection. Claimed work remains explicitly in progress.
                request.cancel({**response, 'ok': False, 'state': 'cancelled'})
            else:
                request.complete(response)
        response = request.wait(0)
        response.setdefault('action', action_name)
        response.setdefault('updated_at', _now_text())
        self._record_action_response(request, response)
        return response


def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def local_ipv4_addresses():
    addresses = []
    try:
        hostnames = {socket.gethostname(), socket.getfqdn()}
        for hostname in hostnames:
            for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
                address = item[4][0]
                if address != '127.0.0.1' and address not in addresses:
                    addresses.append(address)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            address = sock.getsockname()[0]
            if address != '127.0.0.1' and address not in addresses:
                addresses.insert(0, address)
    except OSError:
        pass
    return addresses


REMOTE_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>capLG 手机遥控</title><style>
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#17202a;background:#f3f6f9}body{margin:0;padding:18px}main{max-width:520px;margin:auto}.card{background:#fff;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 3px 14px #0001}h1{font-size:22px;margin:0 0 14px}.muted{color:#667085;font-size:14px}.status{display:grid;grid-template-columns:1fr 1fr;gap:8px}.badge{padding:10px;border-radius:10px;background:#eef2f6;font-size:14px}.badge b{display:block;margin-top:4px}.response{height:220px;overflow:auto;white-space:pre-wrap;background:#101820;color:#d9f0ff;border-radius:10px;padding:12px;font:13px/1.5 Consolas,monospace;margin-bottom:12px}.pair{display:flex;gap:8px}.pair input{min-width:0;flex:1;padding:12px;border:1px solid #ccd5df;border-radius:9px;font-size:18px;letter-spacing:3px}.btn{width:100%;padding:14px 10px;border:0;border-radius:11px;background:#1677ff;color:white;font-size:16px;font-weight:600}.btn:disabled{background:#a9b5c3}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.msg{min-height:22px;margin-top:10px;font-size:14px}.danger{background:#e5484d}.small{font-size:13px}.hidden{display:none}@media(max-width:380px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<div class="card"><h1>📱 capLG 手机遥控</h1><div class="muted">电脑与手机连接同一手机热点后使用</div></div>
<div class="card" id="pairCard"><h2>连接电脑</h2><div class="muted">打开电脑显示的地址即可，通常不需要手动填写端口</div><div class="pair"><input id="code" inputmode="numeric" maxlength="6" placeholder="输入6位配对码"><button class="btn" style="width:auto" onclick="pair()">配对</button></div><div id="pairMsg" class="msg"></div></div>
<div class="card hidden" id="controlCard"><h2>模组响应</h2><div id="response" class="response">等待操作结果…</div><button id="bottomBtn" class="btn hidden" style="margin-bottom:10px" onclick="scrollResponseBottom()">回到底部</button><button class="btn" style="margin-bottom:10px" onclick="clearDisplay()">清空手机显示</button><div class="grid" id="buttons"></div><button class="btn danger" style="margin-top:10px" onclick="unpair()">解除配对</button><div id="actionMsg" class="msg"></div><details><summary>连接信息</summary><div class="status"><div class="badge">模组连接<b id="connected">--</b></div><div class="badge">协议组<b id="profile">--</b></div><div class="badge">操作模式<b id="mode">--</b></div><div class="badge">服务状态<b id="service">--</b></div></div></details></div>
<script>
let token=localStorage.getItem('caplg_token')||'';const labels={face_register:'人脸注册',face_recognize:'人脸识别',palm_register:'手掌注册',palm_recognize:'手掌识别',get_version:'获取版本号',get_users:'获取所有用户ID',download_jpeg:'下载JPEG',download_raw:'下载RAW'};
function show(id,text){document.getElementById(id).textContent=text||''}async function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{});if(token)opt.headers['X-Remote-Token']=token;let r=await fetch(path,opt);return await r.json()}
async function pair(){let code=document.getElementById('code').value.trim();let d=await api('/api/pair',{method:'POST',body:JSON.stringify({code})});if(d.ok){token=d.token;localStorage.setItem('caplg_token',token);show('pairMsg','配对成功');document.getElementById('pairCard').classList.add('hidden');document.getElementById('controlCard').classList.remove('hidden');refresh()}else show('pairMsg',d.message)}
async function autoPair(){let code=new URLSearchParams(location.search).get('code');if(code){document.getElementById('code').value=code;await pair()}}
async function unpair(){await api('/api/unpair',{method:'POST',body:'{}'});token='';localStorage.removeItem('caplg_token');document.getElementById('pairCard').classList.remove('hidden');document.getElementById('controlCard').classList.add('hidden')}
let lastResponse='';function responseAtBottom(){let e=document.getElementById('response');return e.scrollTop+e.clientHeight>=e.scrollHeight-16}function scrollResponseBottom(){let e=document.getElementById('response');e.scrollTop=e.scrollHeight;document.getElementById('bottomBtn').classList.add('hidden')}function clearDisplay(){show('response','手机显示已清空；下一次刷新会继续显示电脑响应窗口内容');lastResponse='';scrollResponseBottom();show('actionMsg','')}
async function action(name){let d=await api('/api/action',{method:'POST',body:JSON.stringify({action:name})});show('actionMsg',d.message||'');refresh()}
async function refresh(){let d=await api('/api/status');if(!d.paired){document.getElementById('pairCard').classList.remove('hidden');document.getElementById('controlCard').classList.add('hidden');return}document.getElementById('pairCard').classList.add('hidden');document.getElementById('controlCard').classList.remove('hidden');show('connected',d.module_connected?'已连接':'未连接');show('profile',d.protocol_profile||'--');show('mode',d.operation_mode||'--');show('service',d.started?'运行中':'已停止');if(d.response_text&&d.response_text!==lastResponse){let follow=responseAtBottom()||!lastResponse;show('response',d.response_text);lastResponse=d.response_text;if(follow)scrollResponseBottom();else document.getElementById('bottomBtn').classList.remove('hidden')}let box=document.getElementById('buttons');if(!box.children.length)Object.keys(labels).forEach(k=>{let b=document.createElement('button');b.className='btn';b.textContent=labels[k];b.onclick=()=>action(k);box.appendChild(b)});if(d.last_result&&d.last_result.message){show('actionMsg',d.last_result.message)}}setInterval(refresh,1500);if(token)refresh();
autoPair();</script></main></body></html>'''
