"""HC-158 Settings 的 canonical 声明、凭据边界、metadata 和 resolver。

本模块把 Plugin Settings 与既有 TOML 配置分开：TOML 只保存非秘密 Harness
配置，Settings 的值只能通过显式注入的 credential backend 访问。默认 backend
是不可用的 fail-closed 实现，因此开发机和测试不会意外读取环境变量、keychain
或明文文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol, Self

try:  # pragma: no cover - Windows 在 CI 外由同一接口覆盖。
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


Scope = Literal["user", "workspace"]
StoreState = Literal[
    "configured",
    "absent",
    "stale",
    "pending",
    "tombstoned",
    "partial",
    "blocked",
]
RuntimeState = Literal["loaded", "not_loaded", "pending_restart", "absent", "stale"]

MAX_SETTING_VALUE_BYTES = 65_536
MAX_SETTING_NAME_BYTES = 256
MAX_SETTING_DESCRIPTION_BYTES = 4_096
MAX_SETTING_ENV_BYTES = 128
SETTINGS_SCHEMA_VERSION = 1
SETTING_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ACCOUNT_RE = re.compile(r"^harness-settings-v1-[0-9a-f]{64}$")
_SAFE_SETTING_ID_RE = re.compile(r"^setting-[0-9a-f]{32}$")
_SAFE_SCOPE_DIGEST_RE = _DIGEST_RE
_SETTINGS_POLICY_VERSION = "settings-policy-v1"
_PROCESS_CONTROL_ENV_NAMES = frozenset(
    {
        "PATH",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "CLASSPATH",
        "RUBYOPT",
        "PERL5OPT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "DYLD_INSERT_LIBRARIES",
    }
)
_PROCESS_CONTROL_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHONWARNINGS")


class SettingsError(ValueError):
    """Settings 领域的稳定错误，不在文案中携带值、路径或 account。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        field: str | None = None,
        retryable: bool = False,
    ) -> None:
        """保存可公开的错误字段；异常字符串只包含稳定 code。"""
        self.code = code
        self.field = field
        self.retryable = retryable
        super().__init__(f"{code}: {message or _DEFAULT_MESSAGES.get(code, 'Settings 操作失败')}")

    def redacted_data(self) -> dict[str, object]:
        """返回 Protocol 可以携带的脱敏 error data。"""
        result: dict[str, object] = {
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.field is not None:
            result["field"] = self.field
        return result


_DEFAULT_MESSAGES: dict[str, str] = {
    "SETTINGS_DECLARATION_INVALID": "Settings declaration 无效",
    "SETTINGS_DECLARATION_AMBIGUOUS": "Settings declaration 不明确",
    "SETTINGS_ENV_FORBIDDEN": "该环境变量不能注入 Plugin 子进程",
    "SETTINGS_VALUE_INVALID": "Settings value 输入无效",
    "SETTINGS_VALUE_TOO_LARGE": "Settings value 超过大小上限",
    "SETTINGS_BACKEND_UNAVAILABLE": "credential backend 不可用",
    "SETTINGS_STORAGE_UNAVAILABLE": "Settings metadata 存储不可用",
    "SETTINGS_SCOPE_INVALID": "Settings scope 无效",
    "SETTINGS_WORKSPACE_SCOPE_REQUIRED": "workspace scope 需要可信 workspace",
    "SETTINGS_RECORD_NOT_FOUND": "Settings record 不存在",
    "SETTINGS_RECORD_STALE": "Settings record 已过期",
    "SETTINGS_DECLARATION_STALE": "Settings declaration 已变化",
    "SETTINGS_STORE_REVISION_CONFLICT": "Settings store revision 冲突",
    "SETTINGS_OPERATION_IN_PROGRESS": "Settings 已有未完成操作",
    "SETTINGS_CLEANUP_PENDING": "Settings cleanup 尚未完成",
    "SETTINGS_UNINSTALL_PARTIAL": "Settings 卸载部分完成",
    "SETTINGS_UNINSTALL_CONFLICT": "Settings 卸载范围已变化",
}


class CredentialBackend(Protocol):
    """最小 credential manager seam；实现不得提供 backend 枚举能力。"""

    def capability_probe(self) -> bool:
        """返回当前 backend 是否已证明可安全使用。"""

    def get(self, account: str) -> str | None:
        """按精确 account 读取一个值。"""

    def set(self, account: str, value: str) -> None:
        """按精确 account 写入一个值。"""

    def delete(self, account: str) -> None:
        """按精确 account 幂等删除一个值。"""


class UnavailableCredentialBackend:
    """默认 backend，明确阻断而不回退环境变量或明文文件。"""

    def capability_probe(self) -> bool:
        """默认不宣称当前平台安全能力。"""
        return False

    def get(self, account: str) -> str | None:
        """不可用 backend 不读取任何内容。"""
        raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")

    def set(self, account: str, value: str) -> None:
        """不可用 backend 不保存任何内容。"""
        raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")

    def delete(self, account: str) -> None:
        """不可用 backend 不执行删除。"""
        raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")


class MacOSCredentialBackend:
    """macOS Security.framework generic-password backend，按需加载且拒绝 iCloud 同步。"""

    _NOT_FOUND = -25300
    _DUPLICATE = -25299

    def __init__(self, *, service: str = "com.za38.harness.settings.v1") -> None:
        """只保存 service；Security.framework 与 probe 都延迟到首次能力检查。"""
        self._service = service
        self._binding: dict[str, object] | None = None
        self._capability: bool | None = None

    def capability_probe(self) -> bool:
        """创建、读取、删除随机 probe item；任一能力无法证明则返回 False。"""
        if self._capability is not None:
            return self._capability
        if sys.platform != "darwin":
            self._capability = False
            return False
        account = "probe-" + secrets.token_hex(16)
        value = secrets.token_urlsafe(24)
        created = False
        try:
            self._load_binding()
            self.set(account, value)
            created = True
            self._capability = self.get(account) == value
        except Exception:
            self._capability = False
        finally:
            if created:
                try:
                    self.delete(account)
                    self._capability = bool(self._capability and self.get(account) is None)
                except Exception:
                    self._capability = False
        return self._capability

    def get(self, account: str) -> str | None:
        """按固定 service/account 精确读取 generic-password data。"""
        binding = self._require_binding()
        try:
            query, owned = binding["query"](account, include_value=True)  # type: ignore[operator]
            try:
                status, data = binding["copy_matching"](query)  # type: ignore[operator]
            finally:
                binding["release_many"](owned + [query])  # type: ignore[operator]
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
        if status == self._NOT_FOUND:
            return None
        if status != 0 or data is None:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
        return data

    def set(self, account: str, value: str) -> None:
        """写入固定 service/account；synchronizable 永远固定为 false。"""
        binding = self._require_binding()
        try:
            query, owned = binding["query"](account, include_value=False, value=value)  # type: ignore[operator]
            try:
                result = binding["add"](query)  # type: ignore[operator]
            finally:
                binding["release_many"](owned + [query])  # type: ignore[operator]
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
        if result not in {0, self._DUPLICATE}:  # duplicate is replaced by an exact delete/add.
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)
        if result == self._DUPLICATE:
            self.delete(account)
            binding = self._require_binding()
            try:
                query, owned = binding["query"](account, include_value=False, value=value)  # type: ignore[operator]
                try:
                    result = binding["add"](query)  # type: ignore[operator]
                finally:
                    binding["release_many"](owned + [query])  # type: ignore[operator]
            except SettingsError:
                raise
            except Exception as exc:
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
            if result != 0:
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)

    def delete(self, account: str) -> None:
        """按固定 service/account 幂等删除。"""
        binding = self._require_binding()
        try:
            query, owned = binding["query"](account, include_value=False)  # type: ignore[operator]
            try:
                result = binding["delete"](query)  # type: ignore[operator]
            finally:
                binding["release_many"](owned + [query])  # type: ignore[operator]
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
        if result not in {0, self._NOT_FOUND}:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)

    def _require_binding(self) -> dict[str, object]:
        """返回已加载的 ctypes binding；不在这里隐式执行 capability probe。"""
        if self._binding is None:
            self._load_binding()
        if self._binding is None:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
        return self._binding

    def _load_binding(self) -> None:
        """延迟建立 Security/CoreFoundation ctypes binding。"""
        if self._binding is not None:
            return
        if sys.platform != "darwin":
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
        try:
            import ctypes

            security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
            core = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            pointer = ctypes.c_void_p
            security.SecItemAdd.argtypes = [pointer, ctypes.POINTER(pointer)]
            security.SecItemAdd.restype = ctypes.c_int32
            security.SecItemCopyMatching.argtypes = [pointer, ctypes.POINTER(pointer)]
            security.SecItemCopyMatching.restype = ctypes.c_int32
            security.SecItemDelete.argtypes = [pointer]
            security.SecItemDelete.restype = ctypes.c_int32
            core.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
            core.CFStringCreateWithCString.restype = pointer
            core.CFDataCreate.argtypes = [pointer, ctypes.c_void_p, ctypes.c_long]
            core.CFDataCreate.restype = pointer
            core.CFDataGetLength.argtypes = [pointer]
            core.CFDataGetLength.restype = ctypes.c_long
            core.CFDataGetBytePtr.argtypes = [pointer]
            core.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
            core.CFDictionaryCreateMutable.argtypes = [pointer, ctypes.c_long, pointer, pointer]
            core.CFDictionaryCreateMutable.restype = pointer
            core.CFDictionarySetValue.argtypes = [pointer, pointer, pointer]
            core.CFDictionarySetValue.restype = None
            core.CFRelease.argtypes = [pointer]
            core.CFRelease.restype = None
            constants = {
                name: ctypes.c_void_p.in_dll(security, name)
                for name in (
                    "kSecClass",
                    "kSecClassGenericPassword",
                    "kSecAttrService",
                    "kSecAttrAccount",
                    "kSecValueData",
                    "kSecReturnData",
                    "kSecAttrSynchronizable",
                )
            }
            constants["kCFBooleanFalse"] = ctypes.c_void_p.in_dll(core, "kCFBooleanFalse")
            constants["kCFBooleanTrue"] = ctypes.c_void_p.in_dll(core, "kCFBooleanTrue")
            constants["kSecMatchLimit"] = ctypes.c_void_p.in_dll(security, "kSecMatchLimit")
            constants["kSecMatchLimitOne"] = ctypes.c_void_p.in_dll(security, "kSecMatchLimitOne")
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc

        import ctypes

        utf8 = 0x08000100

        def cf_string(value: str) -> object:
            return core.CFStringCreateWithCString(None, value.encode("utf-8"), utf8)

        def cf_data(value: str) -> object:
            encoded = value.encode("utf-8")
            buffer = ctypes.create_string_buffer(encoded)
            return core.CFDataCreate(None, buffer, len(encoded))

        def query(
            account: str,
            *,
            include_value: bool,
            value: str | None = None,
        ) -> tuple[object, list[object]]:
            dictionary = core.CFDictionaryCreateMutable(None, 8, None, None)
            if not dictionary:
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
            owned: list[object] = []

            def put(key_name: str, item: object, *, owned_value: bool = False) -> None:
                key = constants[key_name]
                core.CFDictionarySetValue(dictionary, key, item)
                if owned_value:
                    owned.append(item)

            put("kSecClass", constants["kSecClassGenericPassword"])
            put("kSecAttrService", cf_string(self._service), owned_value=True)
            put("kSecAttrAccount", cf_string(account), owned_value=True)
            put("kSecAttrSynchronizable", constants["kCFBooleanFalse"])
            if value is not None:
                put("kSecValueData", cf_data(value), owned_value=True)
            if include_value:
                put("kSecReturnData", constants["kCFBooleanTrue"])
                put("kSecMatchLimit", constants["kSecMatchLimitOne"])
            return dictionary, owned

        def release_many(values: list[object]) -> None:
            for item in values:
                if item:
                    core.CFRelease(item)

        def copy_matching(query_value: object) -> tuple[int, str | None]:
            result = ctypes.c_void_p()
            status = int(security.SecItemCopyMatching(query_value, ctypes.byref(result)))
            if status != 0 or not result.value:
                return status, None
            try:
                length = int(core.CFDataGetLength(result))
                pointer = core.CFDataGetBytePtr(result)
                data = ctypes.string_at(pointer, length).decode("utf-8")
                return status, data
            except (UnicodeDecodeError, ValueError) as exc:
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc
            finally:
                core.CFRelease(result)

        def add(query_value: object) -> int:
            return int(security.SecItemAdd(query_value, None))

        def delete(query_value: object) -> int:
            return int(security.SecItemDelete(query_value))

        self._binding = {
            "query": query,
            "copy_matching": copy_matching,
            "add": add,
            "delete": delete,
            "release_many": release_many,
        }


class LinuxSecretServiceCredentialBackend:
    """Linux Secret Service backend；依赖/session D-Bus 不可证明时 fail closed。"""

    def __init__(self, *, service: str = "com.za38.harness.settings.v1") -> None:
        """只保存 Secret Service attribute namespace。"""
        self._service = service
        self._capability: bool | None = None

    def capability_probe(self) -> bool:
        """用随机 item 完成 create/read/delete probe，不枚举现有 secrets。"""
        if self._capability is not None:
            return self._capability
        if not sys.platform.startswith("linux"):
            self._capability = False
            return False
        account = "probe-" + secrets.token_hex(16)
        value = secrets.token_urlsafe(24)
        created = False
        try:
            self.set(account, value)
            created = True
            self._capability = self.get(account) == value
        except Exception:
            self._capability = False
        finally:
            if created:
                try:
                    self.delete(account)
                    self._capability = bool(self._capability and self.get(account) is None)
                except Exception:
                    self._capability = False
        return self._capability

    @contextmanager
    def _collection(self) -> Iterator[object]:
        """取得默认 collection；锁定、无 D-Bus 或缺依赖均失败关闭。"""
        try:
            import secretstorage

            bus = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(bus)
            if collection.is_locked():
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
            yield collection
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc
        finally:
            close = locals().get("bus")
            if close is not None:
                close_method = getattr(close, "close", None)
                if callable(close_method):
                    try:
                        close_method()
                    except Exception:
                        pass

    def get(self, account: str) -> str | None:
        """通过精确 application/account attributes 读取，不调用 backend 枚举。"""
        try:
            with self._collection() as collection:
                items = tuple(collection.search_items({"application": self._service, "account": account}))  # type: ignore[union-attr]
                if len(items) > 1:
                    raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
                item = items[0] if items else None
                return None if item is None else str(item.get_secret(), "utf-8")
        except Exception as exc:
            if isinstance(exc, SettingsError):
                raise
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc

    def set(self, account: str, value: str) -> None:
        """创建或替换精确 application/account item。"""
        try:
            with self._collection() as collection:
                items = tuple(collection.search_items({"application": self._service, "account": account}))  # type: ignore[union-attr]
                if len(items) > 1:
                    raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
                collection.create_item(  # type: ignore[union-attr]
                    "Harness Settings",
                    {"application": self._service, "account": account},
                    value.encode("utf-8"),
                    replace=True,
                )
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc

    def delete(self, account: str) -> None:
        """精确删除匹配 item；缺失即成功。"""
        try:
            with self._collection() as collection:
                items = tuple(collection.search_items({"application": self._service, "account": account}))  # type: ignore[union-attr]
                if len(items) > 1:
                    raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
                for item in items:
                    item.delete()
        except Exception as exc:
            if isinstance(exc, SettingsError):
                raise
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc


class WindowsCredentialBackend:
    """Windows Credential Manager backend；无可证明的 metadata ACL 时 fail closed。"""

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, *, metadata_root: Path | None = None) -> None:
        """只延迟绑定 advapi32；不使用 DPAPI 或明文文件 fallback。"""
        self._metadata_root = metadata_root
        self._api: dict[str, object] | None = None
        self._capability: bool | None = None

    def capability_probe(self) -> bool:
        """要求 Cred* API 和当前 profile metadata ACL probe 同时成功。"""
        if self._capability is not None:
            return self._capability
        if os.name != "nt":
            self._capability = False
            return False
        account = "probe-" + secrets.token_hex(16)
        value = secrets.token_urlsafe(24)
        created = False
        try:
            self._load_api()
            if not self._probe_metadata_acl():
                self._capability = False
                return False
            self.set(account, value)
            created = True
            self._capability = self.get(account) == value
        except Exception:
            self._capability = False
        finally:
            if created:
                try:
                    self.delete(account)
                    self._capability = bool(self._capability and self.get(account) is None)
                except Exception:
                    self._capability = False
        return self._capability

    def get(self, account: str) -> str | None:
        """按精确 TargetName 读取 Generic credential。"""
        api = self._require_api()
        import ctypes

        pointer = ctypes.c_void_p()
        try:
            read_ok = bool(api["read"](account, ctypes.byref(pointer)))  # type: ignore[operator]
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
        if not read_ok:
            try:
                error = int(api["get_last_error"]())  # type: ignore[operator]
            except Exception as exc:
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
            if error == 1168:  # ERROR_NOT_FOUND
                return None
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)
        try:
            credential = ctypes.cast(pointer, ctypes.POINTER(api["credential_type"])).contents  # type: ignore[index]
            data = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return data.decode("utf-8")
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc
        finally:
            try:
                api["free"](pointer)  # type: ignore[operator]
            except Exception:
                pass

    def set(self, account: str, value: str) -> None:
        """写入 Local-machine persistent scope only after ACL probe. """
        api = self._require_api()
        import ctypes

        try:
            target = ctypes.c_wchar_p(account)
            username = ctypes.c_wchar_p("Harness Settings")
            encoded = value.encode("utf-8")
            blob = ctypes.create_string_buffer(encoded)
            credential = api["credential_type"]()  # type: ignore[operator]
            credential.Type = self._CRED_TYPE_GENERIC
            credential.TargetName = target
            credential.CredentialBlobSize = len(encoded)
            credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
            credential.UserName = username
            if not api["write"](ctypes.byref(credential), 0):  # type: ignore[operator]
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc

    def delete(self, account: str) -> None:
        """按精确 TargetName 幂等删除。"""
        api = self._require_api()
        try:
            deleted = bool(api["delete"](account, self._CRED_TYPE_GENERIC, 0))  # type: ignore[operator]
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
        if not deleted:
            try:
                error = api["get_last_error"]() if "get_last_error" in api else getattr(  # type: ignore[operator]
                    api["kernel"],
                    "GetLastError",
                    lambda: 0,
                )()
            except Exception as exc:
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
            if error not in {1168}:  # ERROR_NOT_FOUND
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)

    def _require_api(self) -> dict[str, object]:
        """取得已加载 API；能力检查由 SettingsStore 在操作前调用。"""
        if self._api is None:
            self._load_api()
        if self._api is None:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
        return self._api

    def _load_api(self) -> None:
        """加载 CredWriteW/CredReadW/CredDeleteW/CredFree 的 ctypes binding。"""
        if self._api is not None:
            return
        if os.name != "nt":
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
        try:
            import ctypes

            class FileTime(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

            class Credential(ctypes.Structure):
                _fields_ = [
                    ("Flags", ctypes.c_uint32),
                    ("Type", ctypes.c_uint32),
                    ("TargetName", ctypes.c_wchar_p),
                    ("Comment", ctypes.c_wchar_p),
                    ("LastWritten", FileTime),
                    ("CredentialBlobSize", ctypes.c_uint32),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", ctypes.c_uint32),
                    ("AttributeCount", ctypes.c_uint32),
                    ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", ctypes.c_wchar_p),
                    ("UserName", ctypes.c_wchar_p),
                ]

            advapi = ctypes.WinDLL("Advapi32.dll")
            kernel = ctypes.WinDLL("Kernel32.dll")
            read = advapi.CredReadW
            read.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
            read.restype = ctypes.c_bool
            write = advapi.CredWriteW
            write.argtypes = [ctypes.POINTER(Credential), ctypes.c_uint32]
            write.restype = ctypes.c_bool
            delete = advapi.CredDeleteW
            delete.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
            delete.restype = ctypes.c_bool
            free = advapi.CredFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = None
            get_named_security_info = advapi.GetNamedSecurityInfoW
            get_named_security_info.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            get_named_security_info.restype = ctypes.c_uint32
            get_token = advapi.OpenProcessToken
            get_token.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
            get_token.restype = ctypes.c_bool
            get_token_info = advapi.GetTokenInformation
            get_token_info.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            get_token_info.restype = ctypes.c_bool
            equal_sid = advapi.EqualSid
            equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            equal_sid.restype = ctypes.c_bool
            get_acl_info = advapi.GetAclInformation
            get_acl_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
            get_acl_info.restype = ctypes.c_bool
            get_ace = advapi.GetAce
            get_ace.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
            get_ace.restype = ctypes.c_bool
            convert_sid = advapi.ConvertStringSidToSidW
            convert_sid.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
            convert_sid.restype = ctypes.c_bool
            local_free = kernel.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            close_handle = kernel.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_bool
            get_current_process = kernel.GetCurrentProcess
            get_current_process.argtypes = []
            get_current_process.restype = ctypes.c_void_p
            self._api = {
                "read": read,
                "write": write,
                "delete": delete,
                "free": free,
                "credential_type": Credential,
                "kernel": kernel,
                "get_named_security_info": get_named_security_info,
                "get_token": get_token,
                "get_token_info": get_token_info,
                "equal_sid": equal_sid,
                "get_acl_info": get_acl_info,
                "get_ace": get_ace,
                "convert_sid": convert_sid,
                "local_free": local_free,
                "close_handle": close_handle,
                "get_current_process": get_current_process,
                "get_last_error": kernel.GetLastError,
            }
        except Exception as exc:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc

    def _probe_metadata_acl(self, target: Path | None = None) -> bool:
        """检查 metadata 目标 owner 与 ACL，无法证明即返回 False。

        ``target`` 存在时检查实际即将读取/替换的 metadata 文件；未提供时只
        在固定 settings 根下创建短命 probe，用于 capability check 和验证新文件
        的继承 ACL。两条路径都不读取 credential value。
        """
        if self._metadata_root is None:
            return False
        api = self._require_api()
        import ctypes

        class AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("AceCount", ctypes.c_uint32),
                ("AclBytesInUse", ctypes.c_uint32),
                ("AclBytesFree", ctypes.c_uint32),
            ]

        class AccessAllowedAce(ctypes.Structure):
            _fields_ = [
                ("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", ctypes.c_uint16),
                ("Mask", ctypes.c_uint32),
                ("SidStart", ctypes.c_uint32),
            ]

        root = self._metadata_root.absolute()
        probe: Path | None = None
        created_probe = False
        security_descriptor = ctypes.c_void_p()
        token = ctypes.c_void_p()
        try:
            if target is None:
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=root,
                    prefix=".capability-",
                    delete=False,
                ) as handle:
                    probe = Path(handle.name)
                created_probe = True
            else:
                probe = target.absolute()
                try:
                    probe.relative_to(root)
                except ValueError:
                    return False
                if probe.is_symlink() or not probe.exists():
                    return False
            owner = ctypes.c_void_p()
            dacl = ctypes.c_void_p()
            # SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1,
            # DACL_SECURITY_INFORMATION=4.
            status = api["get_named_security_info"](  # type: ignore[operator]
                str(probe),
                1,
                1 | 4,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(security_descriptor),
            )
            if status != 0 or not owner.value or not dacl.value:
                return False
            if not api["get_token"](  # type: ignore[operator]
                api["get_current_process"](), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
            ):
                return False
            size = ctypes.c_uint32()
            api["get_token_info"](token, 1, None, 0, ctypes.byref(size))  # TokenUser
            if size.value == 0:
                return False
            token_buffer = ctypes.create_string_buffer(size.value)
            if not api["get_token_info"](  # type: ignore[operator]
                token,
                1,
                token_buffer,
                size.value,
                ctypes.byref(size),
            ):
                return False
            token_sid = ctypes.cast(token_buffer, ctypes.POINTER(ctypes.c_void_p)).contents
            if not api["equal_sid"](owner, token_sid):  # type: ignore[operator]
                return False
            system_sid = ctypes.c_void_p()
            if not api["convert_sid"]("S-1-5-18", ctypes.byref(system_sid)):  # type: ignore[operator]
                return False
            acl_info = AclSizeInformation()
            if not api["get_acl_info"](  # type: ignore[operator]
                dacl,
                ctypes.byref(acl_info),
                ctypes.sizeof(acl_info),
                2,  # AclSizeInformation
            ):
                return False
            for index in range(acl_info.AceCount):
                ace_pointer = ctypes.c_void_p()
                if not api["get_ace"](dacl, index, ctypes.byref(ace_pointer)):  # type: ignore[operator]
                    return False
                ace = ctypes.cast(ace_pointer, ctypes.POINTER(AccessAllowedAce)).contents
                if ace.AceType != 0:
                    continue
                sid_address = ctypes.c_void_p(ctypes.addressof(ace) + AccessAllowedAce.SidStart.offset)
                # 能证明的 allow principal 只有当前 token SID 与 LocalSystem；
                # 任何其他主体（包括非 broad 的普通组）都会使能力 probe 失败。
                if not (
                    api["equal_sid"](sid_address, token_sid)  # type: ignore[operator]
                    or api["equal_sid"](sid_address, system_sid)  # type: ignore[operator]
                ):
                    return False
            return True
        except Exception:
            return False
        finally:
            if token.value:
                api["close_handle"](token)  # type: ignore[operator]
            if security_descriptor.value:
                api["local_free"](security_descriptor)  # type: ignore[operator]
            if "system_sid" in locals() and system_sid.value:
                api["local_free"](system_sid)  # type: ignore[operator]
            if created_probe and probe is not None:
                try:
                    probe.unlink()
                except OSError:
                    pass


def create_platform_credential_backend(
    *,
    metadata_root: Path | None = None,
) -> CredentialBackend:
    """按当前 OS 返回懒加载生产 backend；未知/不可证明平台返回 fail-closed。"""
    if sys.platform == "darwin":
        return MacOSCredentialBackend()
    if sys.platform.startswith("linux"):
        return LinuxSecretServiceCredentialBackend()
    if os.name == "nt":
        return WindowsCredentialBackend(metadata_root=metadata_root)
    return UnavailableCredentialBackend()


class FakeCredentialBackend:
    """只供离线测试的内存 backend，拥有显式调用记录但不支持枚举读取。"""

    def __init__(self, *, available: bool = True) -> None:
        """初始化空 store；fake 值只存于当前测试进程内存。"""
        self.available = available
        self._values: dict[str, str] = {}
        self.operations: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    def capability_probe(self) -> bool:
        """返回测试显式授予的能力。"""
        return self.available

    def get(self, account: str) -> str | None:
        """只按调用方给出的精确 account 读取。"""
        self._maybe_fail("get")
        self.operations.append(("get", account))
        return self._values.get(account)

    def set(self, account: str, value: str) -> None:
        """写入 fake 内存；value 不会进入 operations 或公开摘要。"""
        self._maybe_fail("set")
        self.operations.append(("set", account))
        self._values[account] = value

    def delete(self, account: str) -> None:
        """按精确 account 幂等删除。"""
        self._maybe_fail("delete")
        self.operations.append(("delete", account))
        self._values.pop(account, None)

    @property
    def accounts(self) -> tuple[str, ...]:
        """仅返回测试用 account 名，不暴露值；生产接口没有该属性。"""
        return tuple(sorted(self._values))

    def _maybe_fail(self, operation: str) -> None:
        """允许测试模拟 backend 不可用或 cleanup 失败。"""
        if operation in self.fail_on:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True)


class SimulatedSettingsCrash(RuntimeError):
    """测试用 crash window；保留 durable journal 供下一实例恢复。"""


def _qwen_setting_declaration_digest(env_var: str, sensitive: bool) -> str:
    """计算单个 Qwen setting 的非展示声明 digest。"""
    _validate_env_var(env_var)
    if not isinstance(sensitive, bool):
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="sensitive")
    return _sha256(
        {
            "dialect": "qwen-extension-v1",
            "env_var": env_var,
            "sensitive": sensitive,
            "required": False,
            "consumer_scope": "extension-wide",
        }
    )


@dataclass(frozen=True, slots=True)
class QwenSettingDeclaration:
    """Qwen ExtensionSetting 的严格 canonical 映射。"""

    name: str
    description: str
    env_var: str
    sensitive: bool = False
    required: Literal[False] = False
    setting_key: str = field(init=False)
    declaration_digest: str = field(init=False)

    def __post_init__(self) -> None:
        """固定 exact envVar identity 与不含 display 字段的 declaration digest。"""
        _validate_display_text(self.name, "name", MAX_SETTING_NAME_BYTES)
        _validate_display_text(self.description, "description", MAX_SETTING_DESCRIPTION_BYTES)
        _validate_env_var(self.env_var)
        if not isinstance(self.sensitive, bool):
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="sensitive")
        if self.required is not False:
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="required")
        object.__setattr__(self, "setting_key", self.env_var)
        object.__setattr__(self, "declaration_digest", _qwen_setting_declaration_digest(self.env_var, self.sensitive))

    def to_dict(self) -> dict[str, object]:
        """返回 declaration 摘要；不包含 value。"""
        return {
            "name": self.name,
            "description": self.description,
            "env_var": self.env_var,
            "sensitive": self.sensitive,
            "required": False,
            "setting_key": self.setting_key,
            "declaration_digest": self.declaration_digest,
            "consumer_scope": "extension-wide",
        }


def parse_qwen_setting(value: object) -> QwenSettingDeclaration:
    """严格解析 Qwen ExtensionSetting，不接受 Harness 私有字段。"""
    if not isinstance(value, Mapping):
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="setting")
    allowed = {"name", "description", "envVar", "sensitive"}
    if set(value) - allowed:
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="setting")
    name = value.get("name")
    description = value.get("description")
    env_var = value.get("envVar")
    if not isinstance(name, str) or not isinstance(description, str) or not isinstance(env_var, str):
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="envVar")
    sensitive = value.get("sensitive", False)
    if not isinstance(sensitive, bool):
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="sensitive")
    try:
        return QwenSettingDeclaration(name, description, env_var, sensitive)
    except SettingsError:
        raise
    except (TypeError, ValueError) as exc:
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="envVar") from exc


def parse_qwen_settings(value: object) -> tuple[QwenSettingDeclaration, ...]:
    """解析 Qwen settings 数组并拒绝重复 envVar。"""
    if not isinstance(value, list):
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="settings")
    declarations: list[QwenSettingDeclaration] = []
    seen: set[str] = set()
    for item in value:
        declaration = parse_qwen_setting(item)
        if declaration.env_var in seen:
            raise SettingsError("SETTINGS_DECLARATION_AMBIGUOUS", field="envVar")
        seen.add(declaration.env_var)
        declarations.append(declaration)
    return tuple(declarations)


def qwen_declaration_digest(declarations: Sequence[QwenSettingDeclaration]) -> str:
    """按 envVar/sensitive 计算插件 Settings declaration digest。"""
    return _sha256(
        {
            "dialect": "qwen-extension-v1",
            "settings": [
                {
                    "env_var": item.env_var,
                    "sensitive": item.sensitive,
                    "required": False,
                    "consumer_scope": "extension-wide",
                }
                for item in sorted(declarations, key=lambda item: item.env_var)
            ],
        }
    )


def validate_setting_value(value: object) -> str:
    """验证 Protocol value 与 CLI stdin 共用的 UTF-8/NUL/大小规则。"""
    if isinstance(value, bytes):
        if len(value) > MAX_SETTING_VALUE_BYTES:
            raise SettingsError("SETTINGS_VALUE_TOO_LARGE", field="value")
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SettingsError("SETTINGS_VALUE_INVALID", field="value") from exc
    elif isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SettingsError("SETTINGS_VALUE_INVALID", field="value") from exc
        if len(encoded) > MAX_SETTING_VALUE_BYTES:
            raise SettingsError("SETTINGS_VALUE_TOO_LARGE", field="value")
        decoded = value
    else:
        raise SettingsError("SETTINGS_VALUE_INVALID", field="value")
    if "\x00" in decoded:
        raise SettingsError("SETTINGS_VALUE_INVALID", field="value")
    return decoded


def read_secret_stdin(stream: object) -> str:
    """有界读取 CLI 的一个 stdin record；不接受 argv value 或多行输入。"""
    reader = getattr(stream, "read", None)
    if not callable(reader):
        raise SettingsError("SETTINGS_VALUE_INVALID", field="stdin")
    # 多读一个字节以识别“最大值 + CRLF”，再多出的数据不能被截断后
    # 当成成功；实际读取仍保持有界，不把 stdin 当作可枚举流。
    raw = reader(MAX_SETTING_VALUE_BYTES + 2)
    if isinstance(raw, str):
        try:
            raw_bytes = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SettingsError("SETTINGS_VALUE_INVALID", field="stdin") from exc
    elif isinstance(raw, bytes):
        raw_bytes = raw
    else:
        raise SettingsError("SETTINGS_VALUE_INVALID", field="stdin")
    if len(raw_bytes) > MAX_SETTING_VALUE_BYTES + 2:
        raise SettingsError("SETTINGS_VALUE_TOO_LARGE", field="stdin")

    framing_bytes = 0
    if raw_bytes.endswith(b"\r\n"):
        framing_bytes = 2
    elif raw_bytes.endswith(b"\n"):
        framing_bytes = 1
    value_bytes = raw_bytes[:-framing_bytes] if framing_bytes else raw_bytes
    # 额外内容会把 newline 留在 value_bytes 中，优先报告输入形状错误；
    # 没有 framing/内部换行但超过 byte 上限才归类为 TOO_LARGE。
    if b"\n" in value_bytes or b"\r" in value_bytes:
        raise SettingsError("SETTINGS_VALUE_INVALID", field="stdin")
    if len(value_bytes) > MAX_SETTING_VALUE_BYTES:
        raise SettingsError("SETTINGS_VALUE_TOO_LARGE", field="stdin")
    try:
        raw_value = value_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsError("SETTINGS_VALUE_INVALID", field="stdin") from exc
    if "\n" in raw_value or "\r" in raw_value:
        raise SettingsError("SETTINGS_VALUE_INVALID", field="stdin")
    return validate_setting_value(raw_value)


@dataclass(frozen=True, slots=True)
class SettingBinding:
    """把插件当前声明与 Settings store identity 绑定。"""

    plugin_id: str
    package_digest: str
    declaration_digest: str
    declaration: QwenSettingDeclaration

    @property
    def setting_key(self) -> str:
        """返回 exact Qwen envVar。"""
        return self.declaration.setting_key

    @property
    def env_var(self) -> str:
        """返回供 child overlay 使用的 exact envVar。"""
        return self.declaration.env_var

    @property
    def setting_id(self) -> str:
        """从不可变身份派生 opaque record ID，不使用 display name。"""
        return setting_identity(
            self.plugin_id,
            self.package_digest,
            self.declaration.setting_key,
        )

    def __post_init__(self) -> None:
        """校验 package/declaration 与 exact setting identity。"""
        _validate_digest(self.package_digest, "package_digest")
        _validate_digest(self.declaration_digest, "declaration_digest")
        if self.declaration_digest != self.declaration.declaration_digest:
            raise SettingsError("SETTINGS_DECLARATION_STALE", field="declaration_digest")


def setting_identity(plugin_id: str, package_digest: str, env_var: str) -> str:
    """计算不含 display/path/value 的稳定 setting ID。"""
    _validate_digest(package_digest, "package_digest")
    _validate_env_var(env_var)
    if not isinstance(plugin_id, str) or not plugin_id:
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="plugin_id")
    return "setting-" + _sha256(
        {"plugin_id": plugin_id, "package_digest": package_digest, "env_var": env_var}
    )[:32]


@dataclass(frozen=True, slots=True)
class DurableSettingRecord:
    """只保存可验证身份和 generation，不保存 secret 或派生 runtime 状态。"""

    setting_id: str
    plugin_id: str
    package_digest: str
    declaration_digest: str
    scope: Scope
    scope_binding_digest: str
    name: str
    description: str
    env_var: str
    sensitive: bool
    required: Literal[False]
    consumer_scope: Literal["extension-wide"]
    generation: str

    def to_dict(self) -> dict[str, object]:
        """转换为严格 v1 record；不携带 account/value。"""
        return {
            "setting_id": self.setting_id,
            "plugin_id": self.plugin_id,
            "package_digest": self.package_digest,
            "declaration_digest": self.declaration_digest,
            "scope": self.scope,
            "scope_binding_digest": self.scope_binding_digest,
            "name": self.name,
            "description": self.description,
            "env_var": self.env_var,
            "sensitive": self.sensitive,
            "required": False,
            "consumer_scope": "extension-wide",
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, value: object, *, index_scope: Scope, index_binding: str) -> Self:
        """严格读取 durable record，拒绝未知字段和 runtime 混层。"""
        expected = {
            "setting_id",
            "plugin_id",
            "package_digest",
            "declaration_digest",
            "scope",
            "scope_binding_digest",
            "name",
            "description",
            "env_var",
            "sensitive",
            "required",
            "consumer_scope",
            "generation",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records")
        if value.get("scope") != index_scope or value.get("scope_binding_digest") != index_binding:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records")
        if value.get("required") is not False or value.get("consumer_scope") != "extension-wide":
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records")
        fields = (
            "setting_id",
            "plugin_id",
            "package_digest",
            "declaration_digest",
            "name",
            "description",
            "env_var",
            "generation",
        )
        if any(not isinstance(value.get(item), str) or not value[item] for item in fields):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records")
        if not isinstance(value.get("sensitive"), bool):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records")
        _validate_digest(str(value["package_digest"]), "package_digest")
        _validate_digest(str(value["declaration_digest"]), "declaration_digest")
        _validate_scope_binding(index_binding)
        _validate_env_var(str(value["env_var"]))
        _validate_generation(value["generation"])
        _validate_display_text(str(value["name"]), "name", MAX_SETTING_NAME_BYTES)
        _validate_display_text(str(value["description"]), "description", MAX_SETTING_DESCRIPTION_BYTES)
        try:
            expected_setting_id = setting_identity(
                str(value["plugin_id"]),
                str(value["package_digest"]),
                str(value["env_var"]),
            )
            expected_declaration_digest = _qwen_setting_declaration_digest(
                str(value["env_var"]),
                bool(value["sensitive"]),
            )
        except SettingsError as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records") from exc
        if value["setting_id"] != expected_setting_id or value["declaration_digest"] != expected_declaration_digest:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="records")
        return cls(
            setting_id=str(value["setting_id"]),
            plugin_id=str(value["plugin_id"]),
            package_digest=str(value["package_digest"]),
            declaration_digest=str(value["declaration_digest"]),
            scope=index_scope,
            scope_binding_digest=index_binding,
            name=str(value["name"]),
            description=str(value["description"]),
            env_var=str(value["env_var"]),
            sensitive=bool(value["sensitive"]),
            required=False,
            consumer_scope="extension-wide",
            generation=str(value["generation"]),
        )


@dataclass(frozen=True, slots=True)
class DurableTombstone:
    """持久保留的删除标记，防止旧 retry 复活 record。"""

    setting_id: str
    plugin_id: str
    package_digest: str
    declaration_digest: str
    scope: Scope
    scope_binding_digest: str
    tombstone_generation: str
    committed_revision: int

    def to_dict(self) -> dict[str, object]:
        """转换为不含 secret/account 的 tombstone。"""
        return {
            "setting_id": self.setting_id,
            "plugin_id": self.plugin_id,
            "package_digest": self.package_digest,
            "declaration_digest": self.declaration_digest,
            "scope": self.scope,
            "scope_binding_digest": self.scope_binding_digest,
            "tombstone_generation": self.tombstone_generation,
            "committed_revision": self.committed_revision,
        }


@dataclass(frozen=True, slots=True)
class JournalRef:
    """index 只链接受限 journal 文件，不复制 journal 内部字段。"""

    operation_id: str
    file: str

    def to_dict(self) -> dict[str, str]:
        """转换为安全相对 locator。"""
        return {"operation_id": self.operation_id, "file": self.file}


@dataclass(frozen=True, slots=True)
class WorkspaceRegistryEntry:
    """user index 中的 digest-only workspace scope 登记。"""

    scope_binding_digest: str
    metadata_locator: str
    state: Literal["registering", "registered", "removal_pending", "partial", "removed"]
    registered_revision: int

    def to_dict(self) -> dict[str, object]:
        """转换为不含 workspace 原始路径的 registry entry。"""
        return {
            "scope_binding_digest": self.scope_binding_digest,
            "metadata_locator": self.metadata_locator,
            "state": self.state,
            "registered_revision": self.registered_revision,
        }


@dataclass(frozen=True, slots=True)
class MetadataIndexV1:
    """Settings durable index；store/runtime/pending 派生值不在这里。"""

    scope: Scope
    scope_binding_digest: str
    revision: int = 0
    records: tuple[DurableSettingRecord, ...] = ()
    tombstones: tuple[DurableTombstone, ...] = ()
    journal_refs: tuple[JournalRef, ...] = ()
    workspace_registry: tuple[WorkspaceRegistryEntry, ...] = ()

    def __post_init__(self) -> None:
        """验证顶层 scope、revision 和 workspace registry 边界。"""
        _validate_scope(self.scope)
        _validate_scope_binding(self.scope_binding_digest)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="revision")
        if self.scope == "workspace" and self.workspace_registry:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
        record_ids = tuple(item.setting_id for item in self.records)
        tombstone_ids = tuple(item.setting_id for item in self.tombstones)
        journal_ids = tuple(item.operation_id for item in self.journal_refs)
        workspace_ids = tuple(item.scope_binding_digest for item in self.workspace_registry)
        if (
            len(set(record_ids)) != len(record_ids)
            or len(set(tombstone_ids)) != len(tombstone_ids)
            or len(set(journal_ids)) != len(journal_ids)
            or len(set(workspace_ids)) != len(workspace_ids)
            or set(record_ids) & set(tombstone_ids)
        ):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index")

    def to_dict(self) -> dict[str, object]:
        """输出严格 metadata v1 JSON shape。"""
        return {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "scope": self.scope,
            "scope_binding_digest": self.scope_binding_digest,
            "revision": self.revision,
            "records": [record.to_dict() for record in self.records],
            "tombstones": [item.to_dict() for item in self.tombstones],
            "journal_refs": [item.to_dict() for item in self.journal_refs],
            "workspace_registry": [item.to_dict() for item in self.workspace_registry],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """严格解析 metadata，未知版本/字段/类型全部 fail closed。"""
        expected = {
            "schema_version",
            "scope",
            "scope_binding_digest",
            "revision",
            "records",
            "tombstones",
            "journal_refs",
            "workspace_registry",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index")
        if value.get("schema_version") != SETTINGS_SCHEMA_VERSION:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="schema_version")
        scope = value.get("scope")
        if scope not in {"user", "workspace"}:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="scope")
        binding = value.get("scope_binding_digest")
        if not isinstance(binding, str):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="scope_binding_digest")
        _validate_scope_binding(binding)
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="revision")
        records_raw = value.get("records")
        tombstones_raw = value.get("tombstones")
        refs_raw = value.get("journal_refs")
        registry_raw = value.get("workspace_registry")
        if not all(isinstance(item, list) for item in (records_raw, tombstones_raw, refs_raw, registry_raw)):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index")
        records = tuple(
            DurableSettingRecord.from_dict(item, index_scope=scope, index_binding=binding)
            for item in records_raw
        )
        tombstones = tuple(_tombstone_from_dict(item, scope=scope, binding=binding) for item in tombstones_raw)
        refs = tuple(_journal_ref_from_dict(item) for item in refs_raw)
        registry = tuple(_workspace_entry_from_dict(item) for item in registry_raw)
        return cls(scope, binding, revision, records, tombstones, refs, registry)


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """Host/generation 级 immutable snapshot；值只在当前进程短暂存在。"""

    state: Literal["loaded", "not_loaded"]
    revision: int | None
    generation: str | None
    _values: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    blocked_plugin_ids: frozenset[str] = field(default_factory=frozenset)
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def not_loaded(cls) -> Self:
        """构造尚未启动 Settings resolver 的摘要。"""
        return cls("not_loaded", None, None)

    @classmethod
    def loaded(
        cls,
        revision: int,
        values: Mapping[str, str],
        *,
        blocked_plugin_ids: Sequence[str] = (),
        diagnostics: Sequence[str] = (),
    ) -> Self:
        """构造只属于当前 Host/generation 的内存快照。"""
        return cls(
            "loaded",
            revision,
            secrets.token_hex(16),
            dict(values),
            frozenset(blocked_plugin_ids),
            tuple(dict.fromkeys(diagnostics)),
        )

    def value_for(self, setting_id: str) -> str | None:
        """按 opaque setting ID 读取临时值。"""
        return self._values.get(setting_id)

    def contains(self, setting_id: str) -> bool:
        """判断一个 setting 是否进入当前 runtime snapshot。"""
        return setting_id in self._values

    def to_dict(self) -> dict[str, object]:
        """输出只含状态/revision/generation 的 wire 摘要。"""
        return {
            "state": self.state,
            "revision": self.revision,
            "generation": self.generation,
        }

    def release(self) -> None:
        """best-effort 释放内部引用；Python 字符串无法可靠擦除。"""
        values = self._values
        if isinstance(values, dict):
            values.clear()


@dataclass(frozen=True, slots=True)
class PendingSummary:
    """Protocol 只展示 operation/state/retryable，不暴露 journal 内部字段。"""

    operation: Literal["set", "remove", "uninstall", "migrate"]
    state: Literal["pending", "cleanup_pending", "tombstoned", "partial_retryable", "migrating"]
    retryable: bool

    def to_dict(self) -> dict[str, object]:
        """转换为脱敏摘要。"""
        return {
            "operation": self.operation,
            "state": self.state,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class SettingSummary:
    """settings.list/mutation 共用的脱敏管理面记录。"""

    setting_id: str
    plugin_id: str
    package_digest: str
    declaration_digest: str
    scope: Scope
    scope_binding_digest: str
    source: Literal["qwen-extension"]
    name: str
    description: str
    env_var: str
    sensitive: bool
    required: Literal[False]
    consumer_scope: Literal["extension-wide"]
    store_state: StoreState
    runtime_state: RuntimeState
    pending_operation: PendingSummary | None
    diagnostic: str | None

    def to_dict(self) -> dict[str, object]:
        """转换为完整但不含 value/account/path 的 wire summary。"""
        return {
            "setting_id": self.setting_id,
            "plugin_id": self.plugin_id,
            "package_digest": self.package_digest,
            "declaration_digest": self.declaration_digest,
            "scope": self.scope,
            "scope_binding_digest": self.scope_binding_digest,
            "source": self.source,
            "name": self.name,
            "description": self.description,
            "env_var": self.env_var,
            "sensitive": self.sensitive,
            "required": False,
            "consumer_scope": "extension-wide",
            "store_state": self.store_state,
            "runtime_state": self.runtime_state,
            "pending_operation": (
                self.pending_operation.to_dict() if self.pending_operation is not None else None
            ),
            "diagnostic": self.diagnostic,
        }


class SettingsStore:
    """user/workspace Settings metadata 与 credential account 的事务边界。"""

    def __init__(
        self,
        *,
        home: Path | str | None = None,
        workspace: Path | str | None = None,
        workspace_binding_override: str | None = None,
        backend: CredentialBackend | None = None,
        profile_id: str = "default",
        workspace_roots: Sequence[Path | str] = (),
        policy_version: str = _SETTINGS_POLICY_VERSION,
        failure_injector: Callable[[str], None] | None = None,
        workspace_registry_resolver: Mapping[str, Path | str] | None = None,
    ) -> None:
        """绑定当前 Harness home/profile 和可选可信 workspace，不创建文件。"""
        # 保留调用方给出的 lexical path；realpath 只作为 binding 的另一项输入。
        # 如果这里过早 resolve，直接目录与 symlink 目录会错误共享同一 credential。
        self.home = Path(home or Path.home()).expanduser().absolute()
        # user home 缺失时只建立根目录作为一次性 bootstrap；不创建 Settings
        # metadata/index，因而 list 仍不会产生 store 文件。随后 inode 身份稳定。
        if not self.home.exists():
            try:
                self.home.mkdir(parents=True, exist_ok=True)
            except OSError:
                # 真正的读写操作会以稳定 storage error fail closed。
                pass
        self.workspace = Path(workspace).expanduser().absolute() if workspace is not None else None
        if workspace_binding_override is not None:
            if self.workspace is not None:
                raise SettingsError("SETTINGS_SCOPE_INVALID", field="workspace")
            _validate_scope_binding(workspace_binding_override)
        self.profile_id = profile_id
        if not isinstance(policy_version, str) or not policy_version:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="policy_version")
        self.policy_version = policy_version
        self.backend = backend or UnavailableCredentialBackend()
        self.workspace_roots = tuple(Path(item).expanduser().absolute() for item in workspace_roots)
        self.failure_injector = failure_injector
        self.workspace_registry_resolver = dict(workspace_registry_resolver or {})
        self.settings_root = self.home / ".harness" / "settings" / "v1"
        self._user_binding = scope_binding_digest(
            "user",
            home=self.home,
            profile_id=self.profile_id,
            policy_version=self.policy_version,
        )
        self._workspace_binding = workspace_binding_override or (
            scope_binding_digest(
                "workspace",
                home=self.home,
                workspace=self.workspace,
                profile_id=self.profile_id,
                workspace_roots=self.workspace_roots,
                policy_version=self.policy_version,
            )
            if self.workspace is not None
            else None
        )

    @property
    def user_binding_digest(self) -> str:
        """返回当前 user scope 的本地 digest。"""
        return self._user_binding

    @property
    def workspace_binding_digest(self) -> str:
        """返回当前 workspace scope digest；缺失 workspace 时 fail closed。"""
        if self._workspace_binding is None:
            raise SettingsError("SETTINGS_WORKSPACE_SCOPE_REQUIRED", field="scope")
        return self._workspace_binding

    def list(
        self,
        *,
        scope: Scope,
        declarations: Sequence[SettingBinding] = (),
        snapshot: SettingsSnapshot | None = None,
    ) -> dict[str, object]:
        """读取 live store 与当前 Host snapshot，不在 absent store 建文件。"""
        backend_available = self._backend_is_available()
        binding = self._binding_for_scope(scope)
        index = self._read_or_recover(scope, binding)
        if index is None:
            summaries = [
                self._summary_for_declaration(item, scope, binding, snapshot)
                for item in declarations
            ]
            if not backend_available:
                summaries = [
                    replace(
                        item,
                        store_state="blocked",
                        diagnostic="SETTINGS_BACKEND_UNAVAILABLE",
                    )
                    for item in summaries
                ]
            return {
                "scope": scope,
                "store_revision": 0,
                "runtime_snapshot": (snapshot or SettingsSnapshot.not_loaded()).to_dict(),
                "settings": [item.to_dict() for item in summaries],
            }
        summaries = self._summaries(index, declarations, snapshot, backend_available=backend_available)
        return {
            "scope": scope,
            "store_revision": index.revision,
            "runtime_snapshot": (snapshot or SettingsSnapshot.not_loaded()).to_dict(),
            "settings": [item.to_dict() for item in summaries],
        }

    def set(
        self,
        *,
        scope: Scope,
        plugin_id: str,
        package_digest: str,
        declaration_digest: str,
        setting_key: str,
        env_var: str,
        value: object,
        expected_store_revision: int,
        name: str,
        description: str,
        sensitive: bool = False,
        required: bool = False,
        consumer_scope: str = "extension-wide",
    ) -> dict[str, object]:
        """按 journal/ref → credential → metadata → cleanup 顺序设置一个值。"""
        validated_value = validate_setting_value(value)
        self._validate_mutation_identity(
            scope=scope,
            plugin_id=plugin_id,
            package_digest=package_digest,
            declaration_digest=declaration_digest,
            setting_key=setting_key,
            env_var=env_var,
            name=name,
            description=description,
            sensitive=sensitive,
            required=required,
            consumer_scope=consumer_scope,
        )
        if isinstance(expected_store_revision, bool) or not isinstance(expected_store_revision, int) or expected_store_revision < 0:
            raise SettingsError("SETTINGS_VALUE_INVALID", field="expected_store_revision")
        self._require_backend()
        binding = self._binding_for_scope(scope)
        with self._ordered_mutation_lock(scope):
            index = self._read_or_recover_locked(scope, binding)
            current_revision = index.revision if index is not None else 0
            if current_revision != expected_store_revision:
                raise SettingsError("SETTINGS_STORE_REVISION_CONFLICT", field="expected_store_revision", retryable=True)
            if scope == "workspace":
                self._register_workspace_locked(binding)
            old_record = self._find_record(
                index,
                plugin_id=plugin_id,
                env_var=env_var,
            )
            if old_record is not None and old_record.package_digest == package_digest and old_record.declaration_digest != declaration_digest:
                raise SettingsError("SETTINGS_DECLARATION_STALE", field="declaration_digest")
            operation_id = secrets.token_hex(16)
            new_record = DurableSettingRecord(
                setting_id=setting_identity(plugin_id, package_digest, env_var),
                plugin_id=plugin_id,
                package_digest=package_digest,
                declaration_digest=declaration_digest,
                scope=scope,
                scope_binding_digest=binding,
                name=name,
                description=description,
                env_var=env_var,
                sensitive=sensitive,
                required=False,
                consumer_scope="extension-wide",
                generation=uuid.uuid4().hex,
            )
            new_account = self._account_for(new_record)
            old_account = self._account_for(old_record) if old_record is not None else None
            journal = {
                "schema_version": 1,
                "operation_id": operation_id,
                "operation": "set",
                "phase": "prepared",
                "retryable": True,
                "scope": scope,
                "scope_binding_digest": binding,
                "record": new_record.to_dict(),
                "old_record": old_record.to_dict() if old_record is not None else None,
                "old_generation": old_record.generation if old_record is not None else None,
                "new_generation": new_record.generation,
                "new_account": new_account,
                "old_account": old_account,
            }
            index_with_ref = self._index_with_ref(
                index,
                scope=scope,
                binding=binding,
                operation_id=operation_id,
                revision=(index.revision + 1 if index is not None else 1),
            )
            self._write_journal_and_ref(scope, index_with_ref, journal)
            self._failpoint("set.after_ref")
            try:
                self.backend.set(new_account, validated_value)
            except SettingsError:
                raise
            except Exception as exc:  # pragma: no cover - backend adapter contract
                raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend", retryable=True) from exc
            self._failpoint("set.after_credential")
            journal["phase"] = "credential_written"
            self._write_journal(scope, journal)
            records = tuple(
                record
                for record in (index.records if index is not None else ())
                if record.setting_id
                not in {
                    new_record.setting_id,
                    old_record.setting_id if old_record is not None else "",
                }
            ) + (new_record,)
            committed = self._replace_index(
                index_with_ref,
                revision=index_with_ref.revision + 1,
                records=records,
                tombstones=tuple(
                    item
                    for item in index_with_ref.tombstones
                    if item.setting_id != new_record.setting_id
                ),
            )
            self._write_index(scope, committed)
            self._failpoint("set.after_metadata")
            journal["phase"] = "metadata_committed"
            self._write_journal(scope, journal)
            if old_account is not None and old_account != new_account:
                try:
                    self.backend.delete(old_account)
                except Exception as exc:
                    journal["phase"] = "cleanup_pending"
                    self._write_journal(scope, journal)
                    raise SettingsError("SETTINGS_CLEANUP_PENDING", field="backend", retryable=True) from exc
            finished = self._remove_ref(committed, operation_id)
            self._write_index(scope, finished)
            self._delete_journal(scope, operation_id)
            if scope == "workspace":
                self._mark_workspace_registered_locked(binding)
            return self._mutation_result("set", scope, finished, new_record, snapshot=None)

    def remove(
        self,
        *,
        scope: Scope,
        plugin_id: str,
        package_digest: str,
        declaration_digest: str,
        setting_key: str,
        env_var: str,
        expected_store_revision: int,
        name: str,
        description: str,
        sensitive: bool = False,
    ) -> dict[str, object]:
        """按 prepared → tombstone → 精确 credential cleanup 删除一个值。"""
        self._validate_mutation_identity(
            scope=scope,
            plugin_id=plugin_id,
            package_digest=package_digest,
            declaration_digest=declaration_digest,
            setting_key=setting_key,
            env_var=env_var,
            name=name,
            description=description,
            sensitive=sensitive,
            required=False,
            consumer_scope="extension-wide",
        )
        if isinstance(expected_store_revision, bool) or not isinstance(expected_store_revision, int) or expected_store_revision < 0:
            raise SettingsError("SETTINGS_VALUE_INVALID", field="expected_store_revision")
        self._require_backend()
        binding = self._binding_for_scope(scope)
        with self._ordered_mutation_lock(scope):
            index = self._read_or_recover_locked(scope, binding)
            if index is None:
                raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="setting_key")
            if index.revision != expected_store_revision:
                raise SettingsError("SETTINGS_STORE_REVISION_CONFLICT", field="expected_store_revision", retryable=True)
            record = self._find_exact_record(index, plugin_id, package_digest, declaration_digest, env_var)
            if record is None:
                raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="setting_key")
            operation_id = secrets.token_hex(16)
            tombstone = DurableTombstone(
                setting_id=record.setting_id,
                plugin_id=record.plugin_id,
                package_digest=record.package_digest,
                declaration_digest=record.declaration_digest,
                scope=scope,
                scope_binding_digest=binding,
                tombstone_generation=uuid.uuid4().hex,
                committed_revision=index.revision + 2,
            )
            journal = {
                "schema_version": 1,
                "operation_id": operation_id,
                "operation": "remove",
                "phase": "prepared",
                "retryable": True,
                "scope": scope,
                "scope_binding_digest": binding,
                "record": record.to_dict(),
                "old_generation": record.generation,
                "old_account": self._account_for(record),
                "tombstone": tombstone.to_dict(),
            }
            index_with_ref = self._index_with_ref(
                index,
                scope=scope,
                binding=binding,
                operation_id=operation_id,
                revision=index.revision + 1,
            )
            self._write_journal_and_ref(scope, index_with_ref, journal)
            self._failpoint("remove.after_ref")
            tombstoned = self._replace_index(
                index_with_ref,
                revision=index_with_ref.revision + 1,
                records=tuple(item for item in index.records if item.setting_id != record.setting_id),
                tombstones=index.tombstones + (tombstone,),
            )
            self._write_index(scope, tombstoned)
            self._failpoint("remove.after_tombstone")
            journal["phase"] = "tombstone_committed"
            self._write_journal(scope, journal)
            try:
                self.backend.delete(str(journal["old_account"]))
            except Exception as exc:
                journal["phase"] = "credential_cleanup_pending"
                self._write_journal(scope, journal)
                raise SettingsError("SETTINGS_CLEANUP_PENDING", field="backend", retryable=True) from exc
            journal["phase"] = "credential_cleanup_pending"
            self._write_journal(scope, journal)
            finished = self._remove_ref(tombstoned, operation_id)
            self._write_index(scope, finished)
            self._delete_journal(scope, operation_id)
            return self._mutation_result("remove", scope, finished, record, snapshot=None)

    def rebind_plugin_setting(
        self,
        *,
        old_binding: SettingBinding,
        new_binding: SettingBinding,
    ) -> tuple[str, ...]:
        """在同一 scope 内迁移 update 后仍相同的 setting credential identity。

        value 只在 backend 内部短暂经过，不进入返回值、日志或 metadata。声明
        改变时保持旧记录并返回 warning，避免把旧 secret 猜测注入新声明。
        """
        scope: Scope = "workspace" if self.workspace is not None else "user"
        binding_digest = self._binding_for_scope(scope)
        index = self._read_or_recover(scope, binding_digest)
        if index is None:
            return ()
        record = next(
            (
                item
                for item in index.records
                if item.plugin_id == old_binding.plugin_id
                and item.env_var == old_binding.env_var
            ),
            None,
        )
        if record is None:
            return ()
        if record.package_digest == new_binding.package_digest:
            return ()
        if (
            old_binding.env_var != new_binding.env_var
            or old_binding.declaration.name != new_binding.declaration.name
            or old_binding.declaration_digest != new_binding.declaration_digest
        ):
            return ("PLUGIN_SETTING_RECONFIGURE_REQUIRED",)
        try:
            value = self.backend.get(self._account_for(record))
        except SettingsError:
            raise
        except Exception as exc:  # pragma: no cover - backend adapter contract
            raise SettingsError(
                "SETTINGS_BACKEND_UNAVAILABLE",
                field="backend",
                retryable=True,
            ) from exc
        if value is None:
            return ("SETTINGS_RECORD_STALE",)
        self.set(
            scope=scope,
            plugin_id=new_binding.plugin_id,
            package_digest=new_binding.package_digest,
            declaration_digest=new_binding.declaration_digest,
            setting_key=new_binding.setting_key,
            env_var=new_binding.env_var,
            value=value,
            expected_store_revision=index.revision,
            name=new_binding.declaration.name,
            description=new_binding.declaration.description,
            sensitive=new_binding.declaration.sensitive,
            required=False,
            consumer_scope="extension-wide",
        )
        return ()

    def uninstall_plugin(
        self,
        *,
        plugin_id: str,
        expected_store_revision: int,
        package_digest: str | None = None,
        workspace_stores: Mapping[str, "SettingsStore"] | None = None,
    ) -> dict[str, object]:
        """清理 user record 与 user registry 已登记的全部 workspace scope。

        该方法只允许在 user store 上调用。workspace scope 通过 digest-only
        registry 和调用方提供的受限 resolver 定位；credential backend 永远不参与
        枚举。user lock 先冻结 registry，随后按 digest 顺序取得 workspace locks。
        """
        if self.workspace is not None:
            raise SettingsError("SETTINGS_SCOPE_INVALID", field="uninstall")
        if not isinstance(plugin_id, str) or not plugin_id:
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="plugin_id")
        if package_digest is not None:
            _validate_digest(package_digest, "package_digest")
        self._validate_expected_revision(expected_store_revision)
        self._require_backend()
        resolver = dict(workspace_stores or self.workspace_registry_resolver)
        user_binding = self.user_binding_digest
        removed: list[str] = []
        partial: list[str] = []
        found_target = False
        with _file_lock(self._lock_path("user", user_binding), root=self.home):
            index = self._read_or_recover_locked("user", user_binding)
            if index is None or index.revision != expected_store_revision:
                if index is None:
                    raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="plugin_id")
                raise SettingsError(
                    "SETTINGS_STORE_REVISION_CONFLICT",
                    field="expected_store_revision",
                    retryable=True,
                )
            if not any(record.plugin_id == plugin_id for record in index.records) \
                and not any(item.plugin_id == plugin_id for item in index.tombstones) \
                and not any(entry.state != "removed" for entry in index.workspace_registry):
                raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="plugin_id")

            # tombstone 是删除成功后的长期防复活标记；重试卸载时它仍然
            # 证明该 Plugin 曾在本 scope 有记录，但不再重复写删除事务。
            if any(record.plugin_id == plugin_id for record in index.records) \
                or any(item.plugin_id == plugin_id for item in index.tombstones):
                found_target = True

            if package_digest is not None and any(
                record.plugin_id == plugin_id and record.package_digest != package_digest
                for record in index.records
            ):
                raise SettingsError("SETTINGS_UNINSTALL_CONFLICT", field="package_digest")

            entries = sorted(
                (entry for entry in index.workspace_registry if entry.state != "removed"),
                key=lambda entry: entry.scope_binding_digest,
            )
            if not found_target:
                # registry 只保存 scope digest，不能靠 entry 本身判断 Plugin。
                # 在未知 Plugin 的卸载真正改 registry 前，先读取所有受限
                # metadata scope；存在缺失/损坏就 fail closed，绝不把无关
                # entry 标成 partial。
                workspace_target_found = False
                workspace_unreadable = False
                for entry in entries:
                    try:
                        store = self._workspace_store_for_entry(entry, resolver)
                        if store is None or (
                            store.workspace_binding_digest != entry.scope_binding_digest
                            or store.user_binding_digest != user_binding
                        ):
                            workspace_unreadable = True
                            continue
                        with _file_lock(
                            self._lock_path("workspace", entry.scope_binding_digest),
                            root=self.home,
                        ):
                            workspace_index = store._read_or_recover_locked(  # noqa: SLF001
                                "workspace",
                                entry.scope_binding_digest,
                            )
                        if workspace_index is None:
                            workspace_unreadable = True
                            continue
                        if any(item.plugin_id == plugin_id for item in workspace_index.records) \
                            or any(item.plugin_id == plugin_id for item in workspace_index.tombstones):
                            workspace_target_found = True
                    except SettingsError:
                        workspace_unreadable = True
                if not workspace_target_found:
                    if workspace_unreadable:
                        raise SettingsError(
                            "SETTINGS_STORAGE_UNAVAILABLE",
                            field="workspace_registry",
                            retryable=True,
                        )
                    raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="plugin_id")
                found_target = True

            # 当 user scope 没有目标记录时，上面的预检已经证明至少一个
            # workspace scope 确实属于该 Plugin；否则不会进入 mutation loop。
            # 这避免未知 Plugin 借一个缺失 workspace index 把无关 registry
            # entry 改成 partial。

            # user record 的 remove journal 在当前 user lock 内推进，workspace
            # record 则复用同一精确 remove 状态机但不再次取得 user lock。
            for record in tuple(item for item in index.records if item.plugin_id == plugin_id):
                found_target = True
                try:
                    index = self._remove_record_locked("user", index, record)
                except SettingsError as exc:
                    if exc.code not in {"SETTINGS_CLEANUP_PENDING", "SETTINGS_BACKEND_UNAVAILABLE"}:
                        raise
                    partial.append(f"user:{record.setting_id}")
                    reread = self._read_index("user", user_binding)
                    if reread is None:
                        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index") from exc
                    index = reread
                else:
                    removed.append(f"user:{record.setting_id}")

            for entry in entries:
                store = self._workspace_store_for_entry(entry, resolver)
                if store is None:
                    partial.append(entry.scope_binding_digest)
                    index = self._update_workspace_registry_entry(index, entry, "partial")
                    continue
                try:
                    if (
                        store.workspace_binding_digest != entry.scope_binding_digest
                        or store.user_binding_digest != user_binding
                    ):
                        raise SettingsError("SETTINGS_UNINSTALL_CONFLICT", field="workspace_registry")
                    with _file_lock(
                        self._lock_path("workspace", entry.scope_binding_digest),
                        root=self.home,
                    ):
                        workspace_index = store._read_or_recover_locked(  # noqa: SLF001
                            "workspace", entry.scope_binding_digest
                        )
                        if workspace_index is not None:
                            target_records = tuple(
                                item for item in workspace_index.records if item.plugin_id == plugin_id
                            )
                            target_tombstones = tuple(
                                item for item in workspace_index.tombstones if item.plugin_id == plugin_id
                            )
                            if target_records or target_tombstones:
                                found_target = True
                            if package_digest is not None and any(
                                item.package_digest != package_digest for item in target_records
                            ):
                                raise SettingsError(
                                    "SETTINGS_UNINSTALL_CONFLICT",
                                    field="package_digest",
                                )
                            workspace_failed = False
                            for record in target_records:
                                try:
                                    workspace_index = store._remove_record_locked(  # noqa: SLF001
                                        "workspace", workspace_index, record
                                    )
                                except SettingsError as exc:
                                    if exc.code not in {
                                        "SETTINGS_CLEANUP_PENDING",
                                        "SETTINGS_BACKEND_UNAVAILABLE",
                                    }:
                                        raise
                                    partial.append(entry.scope_binding_digest)
                                    workspace_failed = True
                                    reread = store._read_index(  # noqa: SLF001
                                        "workspace",
                                        entry.scope_binding_digest,
                                    )
                                    if reread is None:
                                        raise SettingsError(
                                            "SETTINGS_STORAGE_UNAVAILABLE",
                                            field="index",
                                        ) from exc
                                    workspace_index = reread
                                    break
                                else:
                                    removed.append(f"workspace:{record.setting_id}")
                            if workspace_failed:
                                index = self._update_workspace_registry_entry(index, entry, "partial")
                            elif (target_records or target_tombstones) and not workspace_index.records:
                                index = self._update_workspace_registry_entry(index, entry, "removed")
                        else:
                            # 已登记但缺失的 scope 无法证明目标 record 已清理，
                            # 固定保留登记并返回 partial，等待 metadata 恢复。
                            partial.append(entry.scope_binding_digest)
                            index = self._update_workspace_registry_entry(index, entry, "partial")
                except SettingsError:
                    partial.append(entry.scope_binding_digest)
                    index = self._update_workspace_registry_entry(index, entry, "partial")

            if not found_target and not partial:
                raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="plugin_id")

            if partial:
                diagnostics = ["SETTINGS_UNINSTALL_PARTIAL"]
            else:
                diagnostics = []
            self._write_index("user", index)
            return {
                "operation": "uninstall",
                "store_revision": index.revision,
                "removed": removed,
                "partial": partial,
                "diagnostics": diagnostics,
            }

    def recover(self, *, scope: Scope) -> None:
        """只按固定 journal 目录恢复；不枚举 credential backend。"""
        self._require_backend()
        binding = self._binding_for_scope(scope)
        with self._ordered_mutation_lock(scope):
            self._read_or_recover_locked(scope, binding)

    def resolve(
        self,
        *,
        bindings: Sequence[SettingBinding],
        snapshot: SettingsSnapshot | None = None,
    ) -> SettingsSnapshot:
        """读取 exact account，按当前 scope 优先级构造 Host/generation snapshot。"""
        if not bindings:
            return SettingsSnapshot.loaded(0, {})
        self._require_backend()
        values: dict[str, str] = {}
        revisions: list[int] = []
        blocked: set[str] = set()
        diagnostics: list[str] = []
        for binding in bindings:
            scopes = ("workspace", "user") if self.workspace is not None else ("user",)
            for scope in scopes:
                binding_digest = self._binding_for_scope(scope)
                index = self._read_or_recover(scope, binding_digest)
                if index is None:
                    continue
                state, value, record = self._resolve_binding_from_index(
                    binding,
                    index,
                    binding_digest,
                )
                if state == "absent" or state == "shadowed":
                    if state == "shadowed":
                        break
                    continue
                if state == "blocked":
                    blocked.add(binding.plugin_id)
                    diagnostics.append(
                        f"plugin:{binding.plugin_id}: {value or 'SETTINGS_RECORD_STALE'}"
                    )
                    break
                assert record is not None and value is not None
                values[binding.setting_id] = value
                revisions.append(index.revision)
                break
        return SettingsSnapshot.loaded(
            max(revisions, default=0),
            values,
            blocked_plugin_ids=blocked,
            diagnostics=diagnostics,
        )

    def _resolve_with_fallback(
        self,
        bindings: Sequence[SettingBinding],
        *,
        fallback: "SettingsStore",
    ) -> SettingsSnapshot:
        """在 workspace store 未配置时回退 user store，保持 workspace 优先。"""
        if not bindings:
            return SettingsSnapshot.loaded(0, {})
        self._require_backend()
        fallback._require_backend()
        values: dict[str, str] = {}
        revisions: list[int] = []
        blocked: set[str] = set()
        diagnostics: list[str] = []
        for binding in bindings:
            selected: tuple[SettingsStore, DurableSettingRecord, str] | None = None
            for store in (self, fallback):
                scope = "workspace" if store.workspace is not None else "user"
                binding_digest = store._binding_for_scope(scope)  # noqa: SLF001 - same domain service
                index = store._read_or_recover(scope, binding_digest)  # noqa: SLF001
                if index is None:
                    continue
                state, value, record = store._resolve_binding_from_index(  # noqa: SLF001
                    binding,
                    index,
                    binding_digest,
                )
                if state == "absent":
                    continue
                if state == "shadowed":
                    break
                if state == "blocked":
                    blocked.add(binding.plugin_id)
                    diagnostics.append(
                        f"plugin:{binding.plugin_id}: {value or 'SETTINGS_RECORD_STALE'}"
                    )
                    break
                assert record is not None and value is not None
                selected = (store, record, value)
                revisions.append(index.revision)
                break
            if selected is not None:
                values[binding.setting_id] = selected[2]
        return SettingsSnapshot.loaded(
            max(revisions, default=0),
            values,
            blocked_plugin_ids=blocked,
            diagnostics=diagnostics,
        )

    def environment_for(
        self,
        *,
        component_kind: str,
        bindings: Sequence[SettingBinding],
        plugin_id: str | None = None,
    ) -> dict[str, str]:
        """只为 MCP/Hook/LSP 生成 child env；Commands/Skills/Agents 返回空集。"""
        if component_kind not in {"mcp", "hooks", "lsp"}:
            return {}
        selected = tuple(
            binding
            for binding in bindings
            if plugin_id is None or binding.plugin_id == plugin_id
        )
        snapshot = self.resolve(bindings=selected)
        try:
            return {
                binding.env_var: value
                for binding in selected
                if (value := snapshot.value_for(binding.setting_id)) is not None
            }
        finally:
            snapshot.release()

    def _resolve_record(self, binding: SettingBinding) -> DurableSettingRecord | None:
        """workspace > user 查找 exact immutable record。"""
        scopes: list[tuple[Scope, str]] = []
        if self.workspace is not None:
            scopes.append(("workspace", self.workspace_binding_digest))
        scopes.append(("user", self.user_binding_digest))
        for scope, binding_digest in scopes:
            index = self._read_or_recover(scope, binding_digest)
            if index is None:
                continue
            record = next((item for item in index.records if item.setting_id == binding.setting_id), None)
            if record is None:
                continue
            if (
                record.package_digest != binding.package_digest
                or record.declaration_digest != binding.declaration_digest
                or record.env_var != binding.env_var
                or record.scope_binding_digest != binding_digest
            ):
                continue
            if self.backend.get(self._account_for(record)) is not None:
                return record
        return None

    def _resolve_binding_from_index(
        self,
        binding: SettingBinding,
        index: MetadataIndexV1,
        binding_digest: str,
    ) -> tuple[
        Literal["absent", "shadowed", "blocked", "loaded"],
        str | None,
        DurableSettingRecord | None,
    ]:
        """把一个 scope 的 record 状态归一化，阻止 stale record 借低优先级值运行。"""
        record = next(
            (item for item in index.records if item.setting_id == binding.setting_id),
            None,
        )
        if record is None:
            if any(item.setting_id == binding.setting_id for item in index.tombstones):
                return "shadowed", None, None
            return "absent", None, None
        if (
            record.package_digest != binding.package_digest
            or record.declaration_digest != binding.declaration_digest
            or record.env_var != binding.env_var
            or record.scope_binding_digest != binding_digest
        ):
            return "blocked", "SETTINGS_RECORD_STALE", record
        try:
            raw_value = self.backend.get(self._account_for(record))
        except SettingsError as exc:
            return "blocked", exc.code, record
        except Exception:
            return "blocked", "SETTINGS_BACKEND_UNAVAILABLE", record
        if raw_value is None:
            return "blocked", "SETTINGS_RECORD_STALE", record
        try:
            value = validate_setting_value(raw_value)
        except SettingsError as exc:
            return "blocked", exc.code, record
        return "loaded", value, record

    def _summary_for_declaration(
        self,
        binding: SettingBinding,
        scope: Scope,
        scope_binding: str,
        snapshot: SettingsSnapshot | None,
    ) -> SettingSummary:
        """为未配置 declaration 派生 absent summary。"""
        return SettingSummary(
            setting_id=binding.setting_id,
            plugin_id=binding.plugin_id,
            package_digest=binding.package_digest,
            declaration_digest=binding.declaration_digest,
            scope=scope,
            scope_binding_digest=scope_binding,
            source="qwen-extension",
            name=binding.declaration.name,
            description=binding.declaration.description,
            env_var=binding.env_var,
            sensitive=binding.declaration.sensitive,
            required=False,
            consumer_scope="extension-wide",
            store_state="absent",
            runtime_state=self._runtime_state(snapshot, binding.setting_id, configured=False),
            pending_operation=None,
            diagnostic=None,
        )

    def _summaries(
        self,
        index: MetadataIndexV1,
        declarations: Sequence[SettingBinding],
        snapshot: SettingsSnapshot | None,
        *,
        backend_available: bool | None = None,
    ) -> tuple[SettingSummary, ...]:
        """将 durable records、live backend 和 Host snapshot 现场组合。"""
        result: list[SettingSummary] = []
        by_id = {item.setting_id: item for item in index.records}
        for binding in declarations:
            record = by_id.get(binding.setting_id)
            if record is None:
                summary = self._summary_for_declaration(
                    binding,
                    index.scope,
                    index.scope_binding_digest,
                    snapshot,
                )
                if any(item.setting_id == binding.setting_id for item in index.tombstones):
                    summary = replace(
                        summary,
                        store_state="tombstoned",
                        diagnostic="SETTINGS_TOMBSTONED",
                    )
                pending = self._pending_for(index, binding.setting_id)
                if pending is not None:
                    summary = replace(
                        summary,
                        store_state="pending" if pending.state == "pending" else "partial",
                        pending_operation=pending,
                        diagnostic="SETTINGS_PENDING",
                    )
                elif backend_available is False:
                    summary = replace(
                        summary,
                        store_state="blocked",
                        diagnostic="SETTINGS_BACKEND_UNAVAILABLE",
                    )
                result.append(summary)
                continue
            result.append(
                self._summary_for_record(
                    record,
                    index,
                    snapshot,
                    backend_available=backend_available,
                )
            )
        declared_ids = {item.setting_id for item in declarations}
        result.extend(
            self._summary_for_record(
                record,
                index,
                snapshot,
                backend_available=backend_available,
            )
            for record in index.records
            if record.setting_id not in declared_ids
        )
        return tuple(sorted(result, key=lambda item: item.setting_id))

    def _summary_for_record(
        self,
        record: DurableSettingRecord,
        index: MetadataIndexV1,
        snapshot: SettingsSnapshot | None,
        *,
        backend_available: bool | None = None,
    ) -> SettingSummary:
        """从 live backend 精确校验 account 后生成脱敏 record。"""
        account = self._account_for(record)
        diagnostic: str | None = None
        if backend_available is False:
            configured = False
            state: StoreState = "blocked"
            diagnostic = "SETTINGS_BACKEND_UNAVAILABLE"
        else:
            try:
                configured = self.backend.get(account) is not None
            except SettingsError as exc:
                configured = False
                state = "blocked"
                diagnostic = exc.code
            except Exception:
                configured = False
                state = "blocked"
                diagnostic = "SETTINGS_BACKEND_UNAVAILABLE"
            else:
                state = "configured" if configured else "stale"
        tombstoned = any(item.setting_id == record.setting_id for item in index.tombstones)
        if tombstoned:
            state = "tombstoned"
            diagnostic = "SETTINGS_TOMBSTONED"
        pending = self._pending_for(index, record.setting_id)
        if pending is not None:
            state = "pending" if pending.state == "pending" else "partial"
            diagnostic = "SETTINGS_PENDING"
        if diagnostic is None and state not in {"configured", "absent"}:
            diagnostic = f"SETTINGS_{state.upper()}"
        return SettingSummary(
            setting_id=record.setting_id,
            plugin_id=record.plugin_id,
            package_digest=record.package_digest,
            declaration_digest=record.declaration_digest,
            scope=record.scope,
            scope_binding_digest=record.scope_binding_digest,
            source="qwen-extension",
            name=record.name,
            description=record.description,
            env_var=record.env_var,
            sensitive=record.sensitive,
            required=False,
            consumer_scope="extension-wide",
            store_state=state,
            runtime_state=self._runtime_state(snapshot, record.setting_id, configured),
            pending_operation=pending,
            diagnostic=diagnostic,
        )

    @staticmethod
    def _runtime_state(snapshot: SettingsSnapshot | None, setting_id: str, configured: bool) -> RuntimeState:
        """区分当前 Host snapshot 与 live store；旧 Host 未刷新时标记 pending_restart。"""
        if snapshot is None or snapshot.state == "not_loaded":
            return "not_loaded"
        if snapshot.contains(setting_id):
            return "loaded"
        return "pending_restart" if configured else "absent"

    def _mutation_result(
        self,
        operation: Literal["set", "remove"],
        scope: Scope,
        index: MetadataIndexV1,
        record: DurableSettingRecord,
        snapshot: SettingsSnapshot | None,
    ) -> dict[str, object]:
        """构造不含 value/account/path 的 mutation result。"""
        summary = self._summary_for_record(record, index, snapshot)
        if operation == "remove":
            summary = replace(
                summary,
                store_state="tombstoned",
                diagnostic="SETTINGS_TOMBSTONED",
            )
        return {
            "operation": operation,
            "scope": scope,
            "store_revision": index.revision,
            "runtime_snapshot": (snapshot or SettingsSnapshot.not_loaded()).to_dict(),
            "summary": summary.to_dict(),
            "diagnostics": [],
        }

    def _validate_mutation_identity(self, **kwargs: object) -> None:
        """统一校验 mutation 的 exact declaration identity 和 denylist。"""
        env_var = kwargs["env_var"]
        setting_key = kwargs["setting_key"]
        if not isinstance(env_var, str) or not isinstance(setting_key, str) or env_var != setting_key:
            raise SettingsError("SETTINGS_DECLARATION_STALE", field="env_var")
        _validate_env_var(env_var)
        for field_name in ("package_digest", "declaration_digest"):
            _validate_digest(str(kwargs[field_name]), field_name)
        if kwargs["required"] is not False:
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="required")
        if kwargs["consumer_scope"] != "extension-wide":
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="consumer_scope")
        if not isinstance(kwargs["sensitive"], bool):
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="sensitive")
        expected_declaration_digest = _qwen_setting_declaration_digest(
            env_var,
            bool(kwargs["sensitive"]),
        )
        if kwargs["declaration_digest"] != expected_declaration_digest:
            raise SettingsError("SETTINGS_DECLARATION_STALE", field="declaration_digest")
        if not isinstance(kwargs["name"], str):
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="name")
        if not isinstance(kwargs["description"], str):
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field="description")
        _validate_display_text(kwargs["name"], "name", MAX_SETTING_NAME_BYTES)
        _validate_display_text(kwargs["description"], "description", MAX_SETTING_DESCRIPTION_BYTES)

    @staticmethod
    def _validate_expected_revision(value: object) -> None:
        """CAS revision 必须是非负整数；HC-158 不提供无条件写入。"""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SettingsError("SETTINGS_VALUE_INVALID", field="expected_store_revision")

    def _binding_for_scope(self, scope: Scope) -> str:
        """按当前 home/workspace 选择 scope binding。"""
        _validate_scope(scope)
        if scope == "workspace":
            return self.workspace_binding_digest
        return self.user_binding_digest

    def _scope_dir(self, scope: Scope, binding: str) -> Path:
        """返回受限 metadata 目录，不解析用户传入的任意路径。"""
        return self.settings_root / ("user" if scope == "user" else "workspaces") / (
            Path() if scope == "user" else Path(binding)
        )

    def _index_path(self, scope: Scope, binding: str) -> Path:
        """返回 canonical index.json。"""
        return self._scope_dir(scope, binding) / "index.json"

    def _lock_path(self, scope: Scope, binding: str) -> Path:
        """返回 scope lock 路径。"""
        return self._scope_dir(scope, binding) / "index.lock"

    def _journal_dir(self, scope: Scope, binding: str) -> Path:
        """返回固定 journal 目录。"""
        return self._scope_dir(scope, binding) / "journal"

    def _read_or_recover(self, scope: Scope, binding: str) -> MetadataIndexV1 | None:
        """读取 index；存在 journal 时通过锁执行恢复。"""
        _ensure_secure_directory(
            self._scope_dir(scope, binding),
            root=self.home,
            create=False,
        )
        path = self._index_path(scope, binding)
        journal_dir = self._journal_dir(scope, binding)
        if path.is_symlink() or journal_dir.is_symlink():
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage")
        if not path.exists() and not journal_dir.exists():
            return None
        with self._ordered_mutation_lock(scope):
            return self._read_or_recover_locked(scope, binding)

    def _read_or_recover_locked(self, scope: Scope, binding: str) -> MetadataIndexV1 | None:
        """在 scope lock 内校验 index/ref/journal 并恢复。"""
        index = self._read_index(scope, binding)
        self._validate_journal_directory(scope, binding)
        if index is None:
            self._remove_safe_orphans(scope, binding, referenced=())
            return None
        for ref in index.journal_refs:
            journal = self._read_journal(scope, binding, ref)
            index = self._replay_journal(scope, binding, index, journal)
        self._remove_safe_orphans(scope, binding, referenced=index.journal_refs)
        return self._read_index(scope, binding)

    def _read_index(self, scope: Scope, binding: str) -> MetadataIndexV1 | None:
        """读取严格 v1 index；不存在时返回 None。"""
        path = self._index_path(scope, binding)
        if path.is_symlink():
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index")
        if not path.exists():
            return None
        _require_regular_file(path, "index")
        _require_windows_metadata_acl(path, root=self.home)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index") from exc
        try:
            index = MetadataIndexV1.from_dict(document)
        except SettingsError as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="index") from exc
        if index.scope != scope or index.scope_binding_digest != binding:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="scope_binding_digest")
        return index

    def _write_index(self, scope: Scope, index: MetadataIndexV1) -> None:
        """用 fsync + atomic replace 写 index。"""
        _atomic_json_write(
            self._index_path(scope, index.scope_binding_digest),
            index.to_dict(),
            root=self.home,
        )
        _require_windows_metadata_acl(
            self._index_path(scope, index.scope_binding_digest),
            root=self.home,
        )

    def _write_journal_and_ref(
        self,
        scope: Scope,
        index: MetadataIndexV1,
        journal: Mapping[str, object],
    ) -> None:
        """固定顺序：完整 journal durable 后才提交 index.journal_refs。"""
        operation_id = str(journal["operation_id"])
        self._write_journal(scope, journal)
        self._failpoint(f"{journal['operation']}.after_journal")
        self._write_index(scope, index)
        self._failpoint(f"{journal['operation']}.after_ref")
        reread = self._read_index(scope, index.scope_binding_digest)
        if reread is None or not any(ref.operation_id == operation_id for ref in reread.journal_refs):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal_refs")

    def _write_journal(self, scope: Scope, journal: Mapping[str, object]) -> None:
        """原子写固定命名 journal；journal 不保存 value。"""
        payload = _validate_journal_payload(
            journal,
            scope=scope,
            binding=self._binding_for_scope(scope),
        )
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str) or not _SAFE_OPERATION_ID_RE.fullmatch(operation_id):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        scope_name = payload.get("scope")
        binding = self._binding_for_scope(scope)
        if scope_name != scope:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        path = self._journal_dir(scope, binding) / f"{operation_id}.json"
        _atomic_json_write(path, payload, root=self.home)
        _require_windows_metadata_acl(path, root=self.home)

    def _read_journal(self, scope: Scope, binding: str, ref: JournalRef) -> dict[str, object]:
        """读取并校验 ref 指向的 journal，拒绝绝对路径/未知文件。"""
        expected = f"journal/{ref.operation_id}.json"
        if ref.file != expected:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal_refs")
        path = self._scope_dir(scope, binding) / ref.file
        _require_regular_file(path, "journal")
        _require_windows_metadata_acl(path, root=self.home)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal") from exc
        try:
            return _validate_journal_payload(value, scope=scope, binding=binding)
        except SettingsError as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal") from exc

    def _replay_journal(
        self,
        scope: Scope,
        binding: str,
        index: MetadataIndexV1,
        journal: dict[str, object],
    ) -> MetadataIndexV1:
        """按 closed operation/phase union 决定唯一回滚或前滚动作。"""
        operation = journal.get("operation")
        phase = journal.get("phase")
        operation_id = journal.get("operation_id")
        if not isinstance(operation_id, str) or not _SAFE_OPERATION_ID_RE.fullmatch(operation_id):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")

        def cleanup_or_pending(account: str, pending_phase: str) -> None:
            """精确删除失败时固定保留 journal，禁止改走另一恢复分支。"""
            try:
                self.backend.delete(account)
            except Exception as exc:
                journal["phase"] = pending_phase
                self._write_journal(scope, journal)
                raise SettingsError("SETTINGS_CLEANUP_PENDING", field="backend", retryable=True) from exc

        if operation == "set":
            record = DurableSettingRecord.from_dict(
                journal["record"],
                index_scope=scope,
                index_binding=binding,
            )
            old_record_raw = journal["old_record"]
            old_record = (
                None
                if old_record_raw is None
                else DurableSettingRecord.from_dict(
                    old_record_raw,
                    index_scope=scope,
                    index_binding=binding,
                )
            )
            active = next((item for item in index.records if item.setting_id == record.setting_id), None)
            new_active = active is not None and active.generation == record.generation
            old_active = (
                old_record is not None
                and active is not None
                and active.setting_id == old_record.setting_id
                and active.generation == old_record.generation
            )
            if active is not None and not new_active and not old_active:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            new_account = _validate_account(journal["new_account"], "new_account")
            old_account = (
                None
                if journal["old_account"] is None
                else _validate_account(journal["old_account"], "old_account")
            )

            def credential_exists(account: str) -> bool:
                """只按 journal 指定 account 验证存在性，不读取或枚举其他值。"""
                try:
                    return self.backend.get(account) is not None
                except SettingsError:
                    raise
                except Exception as exc:  # pragma: no cover - backend adapter contract
                    raise SettingsError(
                        "SETTINGS_BACKEND_UNAVAILABLE",
                        field="backend",
                        retryable=True,
                    ) from exc

            def forward_new_record() -> MetadataIndexV1:
                """index 已观察到或 journal 已证明 new commit 时前滚。"""
                nonlocal index
                if not credential_exists(new_account):
                    raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
                record_ids = {record.setting_id}
                if old_record is not None:
                    record_ids.add(old_record.setting_id)
                records = tuple(item for item in index.records if item.setting_id not in record_ids) + (record,)
                if records != index.records:
                    index = self._replace_index(index, revision=index.revision + 1, records=records)
                    self._write_index(scope, index)
                if old_account is not None:
                    cleanup_or_pending(old_account, "cleanup_pending")
                finished = self._remove_ref(index, operation_id)
                self._write_index(scope, finished)
                self._delete_journal(scope, operation_id)
                return finished

            # index 已落到 new generation 时，即使 journal phase 仍是
            # prepared/credential_written，也只能前滚；这是 metadata commit
            # 与 phase 写入之间崩溃窗口的唯一安全裁决。
            if new_active:
                return forward_new_record()

            # old generation 仍 active 只可能处于 metadata 尚未提交的回滚分支。
            # cleanup_pending 在该分支表示精确删除 new account 失败，不能误前滚。
            if old_active and phase in {"prepared", "credential_written", "cleanup_pending"}:
                cleanup_or_pending(new_account, "cleanup_pending")
                finished = self._remove_ref(index, operation_id)
                self._write_index(scope, finished)
                self._delete_journal(scope, operation_id)
                return finished

            if phase in {"prepared", "credential_written"}:
                cleanup_or_pending(new_account, "cleanup_pending")
                finished = self._remove_ref(index, operation_id)
                self._write_index(scope, finished)
                self._delete_journal(scope, operation_id)
                return finished

            if phase == "metadata_committed":
                return forward_new_record()

            # 没有 old/new active 且 cleanup_pending 无法证明属于哪一个
            # commit window；除初次 set 的回滚清理外一律 fail closed。
            if phase == "cleanup_pending" and old_record is None:
                cleanup_or_pending(new_account, "cleanup_pending")
                finished = self._remove_ref(index, operation_id)
                self._write_index(scope, finished)
                self._delete_journal(scope, operation_id)
                return finished

            if phase not in {"metadata_committed", "cleanup_pending"}:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")

        if operation == "remove":
            record = DurableSettingRecord.from_dict(
                journal["record"],
                index_scope=scope,
                index_binding=binding,
            )
            tombstone = _tombstone_from_dict(
                journal["tombstone"],
                scope=scope,
                binding=binding,
            )
            old_account = _validate_account(journal["old_account"], "old_account")
            active = next((item for item in index.records if item.setting_id == record.setting_id), None)
            if active is not None and active.generation != record.generation:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            existing_tombstone = next(
                (item for item in index.tombstones if item.setting_id == tombstone.setting_id),
                None,
            )
            if existing_tombstone is not None and existing_tombstone != tombstone:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            if phase == "prepared" and existing_tombstone is None:
                finished = self._remove_ref(index, operation_id)
                self._write_index(scope, finished)
                self._delete_journal(scope, operation_id)
                return finished
            if phase not in {"tombstone_committed", "credential_cleanup_pending"} and existing_tombstone is None:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            if existing_tombstone is None:
                index = self._replace_index(
                    index,
                    revision=index.revision + 1,
                    records=tuple(item for item in index.records if item.setting_id != record.setting_id),
                    tombstones=index.tombstones + (tombstone,),
                )
                self._write_index(scope, index)
            journal["phase"] = "credential_cleanup_pending"
            self._write_journal(scope, journal)
            cleanup_or_pending(old_account, "credential_cleanup_pending")
            finished = self._remove_ref(index, operation_id)
            self._write_index(scope, finished)
            self._delete_journal(scope, operation_id)
            return finished
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")

    def _validate_journal_directory(self, scope: Scope, binding: str) -> None:
        """只接受安全 journal 文件和临时文件，任何未知项 fail closed。"""
        directory = self._journal_dir(scope, binding)
        if directory.is_symlink():
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        if not directory.exists():
            return
        if not directory.is_dir():
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        _require_windows_metadata_acl(directory, root=self.home)
        for entry in directory.iterdir():
            if entry.is_symlink():
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                if not re.fullmatch(r"\.[0-9a-f]{32}\.json\.[A-Za-z0-9_-]{8}\.tmp", entry.name):
                    raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
                _require_regular_file(entry, "journal")
                _require_windows_metadata_acl(entry, root=self.home)
                continue
            if not re.fullmatch(r"[0-9a-f]{32}\.json", entry.name) or not entry.is_file():
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            _require_regular_file(entry, "journal")
            _require_windows_metadata_acl(entry, root=self.home)

    def _remove_safe_orphans(self, scope: Scope, binding: str, *, referenced: Sequence[JournalRef]) -> None:
        """删除未被 refs 引用的安全 orphan，不触碰 credential backend。"""
        directory = self._journal_dir(scope, binding)
        if not directory.exists():
            return
        referenced_names = {ref.file.rsplit("/", 1)[-1] for ref in referenced}
        for entry in directory.iterdir():
            if entry.name in referenced_names:
                continue
            if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                entry.unlink()
            elif re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                entry.unlink()

    def _delete_journal(self, scope: Scope, operation_id: str) -> None:
        """删除已解除 ref 的 journal；失败保持 storage error。"""
        binding = self._binding_for_scope(scope)
        path = self._journal_dir(scope, binding) / f"{operation_id}.json"
        if path.is_symlink():
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        if path.exists():
            _require_regular_file(path, "journal")
            _require_windows_metadata_acl(path, root=self.home)
            try:
                path.unlink()
            except OSError as exc:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal") from exc

    def _mutation_lock(self, scope: Scope) -> Iterator[None]:
        """返回单 scope lock；workspace 操作使用下方固定的 user→workspace 顺序。"""
        return _file_lock(
            self._lock_path(scope, self._binding_for_scope(scope)),
            root=self.home,
        )

    @contextmanager
    def _ordered_mutation_lock(self, scope: Scope) -> Iterator[None]:
        """按 canonical 顺序持锁，避免 workspace registry 与 scope 交叉死锁。"""
        binding = self._binding_for_scope(scope)
        if scope == "workspace":
            with _file_lock(self._lock_path("user", self.user_binding_digest), root=self.home):
                with _file_lock(self._lock_path("workspace", binding), root=self.home):
                    yield
            return
        with _file_lock(self._lock_path("user", binding), root=self.home):
            yield

    def _register_workspace_locked(self, workspace_binding: str) -> None:
        """在 workspace mutation 前登记 digest-only scope，状态先为 registering。"""
        user_binding = self.user_binding_digest
        index = self._read_or_recover_locked("user", user_binding)
        if index is None:
            index = MetadataIndexV1("user", user_binding)
        existing = next(
            (item for item in index.workspace_registry if item.scope_binding_digest == workspace_binding),
            None,
        )
        if existing is not None and existing.state == "registered":
            return
        entry = WorkspaceRegistryEntry(
            scope_binding_digest=workspace_binding,
            metadata_locator=f"workspaces/{workspace_binding}/index.json",
            state="registering",
            registered_revision=index.revision + 1,
        )
        entries = tuple(
            entry if item.scope_binding_digest == workspace_binding else item
            for item in index.workspace_registry
        )
        if existing is None:
            entries += (entry,)
        self._write_index(
            "user",
            self._replace_index(
                index,
                revision=index.revision + 1,
                workspace_registry=entries,
            ),
        )

    def _mark_workspace_registered_locked(self, workspace_binding: str) -> None:
        """workspace set 完成后把同一 digest 的 registry entry 原子推进 registered。"""
        user_binding = self.user_binding_digest
        index = self._read_or_recover_locked("user", user_binding)
        if index is None:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
        existing = next(
            (item for item in index.workspace_registry if item.scope_binding_digest == workspace_binding),
            None,
        )
        if existing is None:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
        if existing.state == "registered":
            return
        replacement = replace(
            existing,
            state="registered",
            registered_revision=index.revision + 1,
        )
        entries = tuple(
            replacement if item.scope_binding_digest == workspace_binding else item
            for item in index.workspace_registry
        )
        self._write_index(
            "user",
            self._replace_index(
                index,
                revision=index.revision + 1,
                workspace_registry=entries,
            ),
        )

    @staticmethod
    def _update_workspace_registry_entry(
        index: MetadataIndexV1,
        entry: WorkspaceRegistryEntry,
        state: Literal["partial", "removed"],
    ) -> MetadataIndexV1:
        """更新 user index 中一个 scope 的卸载状态，不改变其他登记。"""
        replacement = replace(
            entry,
            state=state,
            registered_revision=index.revision + 1,
        )
        return SettingsStore._replace_index(
            index,
            revision=index.revision + 1,
            workspace_registry=tuple(
                replacement if item.scope_binding_digest == entry.scope_binding_digest else item
                for item in index.workspace_registry
            ),
        )

    def _workspace_store_for_entry(
        self,
        entry: WorkspaceRegistryEntry,
        resolver: Mapping[str, object],
    ) -> "SettingsStore" | None:
        """把受限 resolver 的 digest 映射成 workspace store；不接受任意路径输入。"""
        candidate = resolver.get(entry.scope_binding_digest)
        if isinstance(candidate, SettingsStore):
            return candidate
        if isinstance(candidate, (Path, str)):
            return SettingsStore(
                home=self.home,
                workspace=candidate,
                backend=self.backend,
                profile_id=self.profile_id,
                workspace_roots=self.workspace_roots,
                policy_version=self.policy_version,
            )
        return SettingsStore(
            home=self.home,
            workspace_binding_override=entry.scope_binding_digest,
            backend=self.backend,
            profile_id=self.profile_id,
            workspace_roots=self.workspace_roots,
            policy_version=self.policy_version,
        )

    def _remove_record_locked(
        self,
        scope: Scope,
        index: MetadataIndexV1,
        record: DurableSettingRecord,
    ) -> MetadataIndexV1:
        """在调用方已持有 canonical lock 时，复用 remove 的完整 journal 状态机。"""
        operation_id = secrets.token_hex(16)
        tombstone = DurableTombstone(
            setting_id=record.setting_id,
            plugin_id=record.plugin_id,
            package_digest=record.package_digest,
            declaration_digest=record.declaration_digest,
            scope=scope,
            scope_binding_digest=index.scope_binding_digest,
            tombstone_generation=uuid.uuid4().hex,
            committed_revision=index.revision + 2,
        )
        journal = {
            "schema_version": 1,
            "operation_id": operation_id,
            "operation": "remove",
            "phase": "prepared",
            "retryable": True,
            "scope": scope,
            "scope_binding_digest": index.scope_binding_digest,
            "record": record.to_dict(),
            "old_generation": record.generation,
            "old_account": self._account_for(record),
            "tombstone": tombstone.to_dict(),
        }
        index_with_ref = self._index_with_ref(
            index,
            scope=scope,
            binding=index.scope_binding_digest,
            operation_id=operation_id,
            revision=index.revision + 1,
        )
        self._write_journal_and_ref(scope, index_with_ref, journal)
        tombstoned = self._replace_index(
            index_with_ref,
            revision=index_with_ref.revision + 1,
            records=tuple(item for item in index.records if item.setting_id != record.setting_id),
            tombstones=index.tombstones + (tombstone,),
        )
        self._write_index(scope, tombstoned)
        journal["phase"] = "tombstone_committed"
        self._write_journal(scope, journal)
        try:
            self.backend.delete(str(journal["old_account"]))
        except Exception as exc:
            journal["phase"] = "credential_cleanup_pending"
            self._write_journal(scope, journal)
            raise SettingsError("SETTINGS_CLEANUP_PENDING", field="backend", retryable=True) from exc
        journal["phase"] = "credential_cleanup_pending"
        self._write_journal(scope, journal)
        finished = self._remove_ref(tombstoned, operation_id)
        self._write_index(scope, finished)
        self._delete_journal(scope, operation_id)
        return finished

    def _require_backend(self) -> None:
        """没有显式可用 credential backend 时立即 fail closed。"""
        try:
            available = self.backend.capability_probe()
        except Exception as exc:  # pragma: no cover - adapter boundary
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend") from exc
        if not available:
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")

    def _backend_is_available(self) -> bool:
        """管理面只探测 backend 能力并保留 blocked 摘要，不抛弃 declaration。"""
        try:
            return bool(self.backend.capability_probe())
        except Exception:
            return False

    def _find_record(self, index: MetadataIndexV1 | None, *, plugin_id: str, env_var: str) -> DurableSettingRecord | None:
        """找到同插件同 envVar 的 active record，用于 declaration drift 判断。"""
        if index is None:
            return None
        return next((item for item in index.records if item.plugin_id == plugin_id and item.env_var == env_var), None)

    @staticmethod
    def _find_exact_record(
        index: MetadataIndexV1,
        plugin_id: str,
        package_digest: str,
        declaration_digest: str,
        env_var: str,
    ) -> DurableSettingRecord | None:
        """按 immutable identity 精确查找，不按 display name 或 wildcard。"""
        return next(
            (
                item
                for item in index.records
                if item.plugin_id == plugin_id
                and item.package_digest == package_digest
                and item.declaration_digest == declaration_digest
                and item.env_var == env_var
            ),
            None,
        )

    def _account_for(self, record: DurableSettingRecord | None) -> str:
        """由 scope/identity/generation 派生 exact account；不持久化在 record。"""
        if record is None:
            raise SettingsError("SETTINGS_RECORD_NOT_FOUND", field="setting_key")
        return _expected_account_for_record(record)

    @staticmethod
    def _index_with_ref(
        index: MetadataIndexV1 | None,
        *,
        scope: Scope,
        binding: str,
        operation_id: str,
        revision: int,
    ) -> MetadataIndexV1:
        """创建带 ref 的 v1 index；absent bootstrap 只发生在 set。"""
        ref = JournalRef(operation_id, f"journal/{operation_id}.json")
        return MetadataIndexV1(
            scope=scope,
            scope_binding_digest=binding,
            revision=revision,
            records=index.records if index is not None else (),
            tombstones=index.tombstones if index is not None else (),
            journal_refs=((index.journal_refs if index is not None else ()) + (ref,)),
            workspace_registry=index.workspace_registry if index is not None else (),
        )

    @staticmethod
    def _replace_index(
        index: MetadataIndexV1,
        *,
        revision: int,
        records: tuple[DurableSettingRecord, ...] | None = None,
        tombstones: tuple[DurableTombstone, ...] | None = None,
        workspace_registry: tuple[WorkspaceRegistryEntry, ...] | None = None,
    ) -> MetadataIndexV1:
        """复制 index 的 durable 部分，不引入 runtime 字段。"""
        return MetadataIndexV1(
            scope=index.scope,
            scope_binding_digest=index.scope_binding_digest,
            revision=revision,
            records=index.records if records is None else records,
            tombstones=index.tombstones if tombstones is None else tombstones,
            journal_refs=index.journal_refs,
            workspace_registry=(
                index.workspace_registry
                if workspace_registry is None
                else workspace_registry
            ),
        )

    @staticmethod
    def _remove_ref(index: MetadataIndexV1, operation_id: str) -> MetadataIndexV1:
        """从 index 原子移除 ref，随后才允许删除 journal 文件。"""
        refs = tuple(ref for ref in index.journal_refs if ref.operation_id != operation_id)
        return MetadataIndexV1(
            scope=index.scope,
            scope_binding_digest=index.scope_binding_digest,
            revision=index.revision + 1,
            records=index.records,
            tombstones=index.tombstones,
            journal_refs=refs,
            workspace_registry=index.workspace_registry,
        )

    def _pending_for(self, index: MetadataIndexV1, setting_id: str) -> PendingSummary | None:
        """由 refs 中的 closed union 派生脱敏 pending summary。"""
        binding = index.scope_binding_digest
        for ref in index.journal_refs:
            try:
                journal = self._read_journal(index.scope, binding, ref)
            except SettingsError:
                return PendingSummary("set", "partial_retryable", True)
            record = journal.get("record")
            if isinstance(record, Mapping) and record.get("setting_id") != setting_id:
                continue
            operation = journal.get("operation")
            phase = journal.get("phase")
            if operation == "set":
                return PendingSummary(
                    "set",
                    "cleanup_pending" if phase == "cleanup_pending" else "pending",
                    True,
                )
            if operation == "remove":
                return PendingSummary(
                    "remove",
                    "tombstoned" if phase != "prepared" else "pending",
                    True,
                )
            return PendingSummary("migrate", "partial_retryable", True)
        return None

    def _failpoint(self, name: str) -> None:
        """执行测试注入点；生产实例不设置 callback。"""
        if self.failure_injector is not None:
            self.failure_injector(name)


class SettingsResolver:
    """把一个 Host/generation 的 declaration bindings 解析成 child overlay。"""

    def __init__(self, *, user: SettingsStore, workspace: SettingsStore | None = None) -> None:
        """绑定 user 与可信 workspace store；不读取 shell 环境。"""
        self.user = user
        self.workspace = workspace

    def resolve(self, bindings: Sequence[SettingBinding]) -> SettingsSnapshot:
        """workspace > user 解析并创建不可变 snapshot。"""
        if self.workspace is not None:
            return self.workspace._resolve_with_fallback(bindings, fallback=self.user)
        return self.user.resolve(bindings=bindings)

    def environment_for(
        self,
        component_kind: str,
        bindings: Sequence[SettingBinding],
        *,
        plugin_id: str | None = None,
    ) -> dict[str, str]:
        """仅给 MCP/Hook/LSP 子进程最小声明 env，其他 consumer 恒为空。"""
        if component_kind not in {"mcp", "hooks", "lsp"}:
            return {}
        selected = tuple(
            binding
            for binding in bindings
            if plugin_id is None or binding.plugin_id == plugin_id
        )
        snapshot = self.resolve(selected)
        try:
            return {
                binding.env_var: value
                for binding in selected
                if (value := snapshot.value_for(binding.setting_id)) is not None
            }
        finally:
            snapshot.release()


def scope_binding_digest(
    scope: Scope,
    *,
    home: Path,
    workspace: Path | None = None,
    profile_id: str = "default",
    workspace_roots: Sequence[Path] = (),
    policy_version: str = _SETTINGS_POLICY_VERSION,
) -> str:
    """计算绑定 digest；原始 home/workspace 只参与本地 hash，不落盘/出 wire。"""
    _validate_scope(scope)
    if scope == "user":
        payload = {
            "domain": "harness-settings-user-v1",
            "home_identity": _home_binding_identity(home),
            "profile_id": profile_id,
            "os_user": _current_user_principal(),
            "backend_namespace": "current-user-credential-manager-v1",
            "policy_version": policy_version,
        }
    else:
        if workspace is None:
            raise SettingsError("SETTINGS_WORKSPACE_SCOPE_REQUIRED", field="scope")
        payload = {
            "domain": "harness-settings-workspace-v1",
            "workspace_identity": _path_identity(workspace),
            "workspace_realpath_identity": _path_identity(workspace.resolve()),
            "trusted_roots": [_path_identity(item) for item in workspace_roots],
            "profile_id": profile_id,
            "user_binding": scope_binding_digest(
                "user",
                home=home,
                profile_id=profile_id,
                policy_version=policy_version,
            ),
            "policy_version": policy_version,
        }
    return _sha256(payload)


def _validate_scope(scope: object) -> None:
    """验证闭集 scope。"""
    if scope not in {"user", "workspace"}:
        raise SettingsError("SETTINGS_SCOPE_INVALID", field="scope")


def _validate_scope_binding(value: str) -> None:
    """验证不含原始路径的 scope digest。"""
    if not isinstance(value, str) or _SAFE_SCOPE_DIGEST_RE.fullmatch(value) is None:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="scope_binding_digest")


def _validate_digest(value: str, field_name: str) -> None:
    """验证 package/declaration digest 的固定形状。"""
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field=field_name)


def _validate_generation(value: object, field_name: str = "generation") -> str:
    """验证 journal/record 使用的不可变 generation 形状。"""
    if not isinstance(value, str) or _SAFE_GENERATION_RE.fullmatch(value) is None:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field=field_name)
    return value


def _validate_account(value: object, field_name: str = "account") -> str:
    """验证 journal 中的 opaque account 形状，不验证或暴露其值。"""
    if not isinstance(value, str) or _SAFE_ACCOUNT_RE.fullmatch(value) is None:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field=field_name)
    return value


def _validate_display_text(value: str, field_name: str, max_bytes: int) -> None:
    """验证展示字段，不做 identity 归一化。"""
    if not isinstance(value, str) or not value.strip():
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field=field_name)
    try:
        if len(value.encode("utf-8")) > max_bytes:
            raise SettingsError("SETTINGS_DECLARATION_INVALID", field=field_name)
    except UnicodeEncodeError as exc:
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field=field_name) from exc


def _validate_env_var(value: str) -> None:
    """验证 Qwen exact envVar 和 process-control denylist。"""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_SETTING_ENV_BYTES or not SETTING_ENV_VAR_RE.fullmatch(value):
        raise SettingsError("SETTINGS_DECLARATION_INVALID", field="envVar")
    if value in _PROCESS_CONTROL_ENV_NAMES or any(value.startswith(prefix) for prefix in _PROCESS_CONTROL_ENV_PREFIXES):
        raise SettingsError("SETTINGS_ENV_FORBIDDEN", field="envVar")


def _path_identity(path: Path, *, include_filesystem: bool = True) -> dict[str, object]:
    """绑定 lexical path、realpath 和可选现存目录 inode，拒绝静默复用旧 scope。"""
    lexical = path.absolute()
    resolved = lexical.resolve(strict=False)

    def filesystem_identity(candidate: Path, *, follow_symlink: bool) -> dict[str, int] | None:
        try:
            info = candidate.stat() if follow_symlink else candidate.lstat()
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        return {"device": int(info.st_dev), "inode": int(info.st_ino)}

    identity: dict[str, object] = {
        "lexical_path_hash": _sha256(str(lexical)),
        "real_path_hash": _sha256(str(resolved)),
    }
    if include_filesystem:
        identity.update(
            {
                "lexical_filesystem": filesystem_identity(lexical, follow_symlink=False),
                "real_filesystem": filesystem_identity(resolved, follow_symlink=True),
            }
        )
    return identity


def _home_binding_identity(path: Path) -> dict[str, object]:
    """为 user scope 绑定现有 home 身份，并让首次创建沿用稳定 bootstrap 身份。"""
    lexical = path.absolute()
    try:
        info = lexical.lstat()
    except OSError:
        info = None
    if info is not None and (stat.S_ISDIR(info.st_mode) or lexical.is_dir()):
        return _path_identity(lexical, include_filesystem=True)
    # 缺失 home 尚无 inode；绑定父目录 inode 与最终名称，mkdir 后仍保持同一
    # identity，同时父目录替换会让既有 profile 失效而不是静默复用。
    parent = lexical.parent
    try:
        parent_info = parent.stat()
        parent_filesystem = {
            "device": int(parent_info.st_dev),
            "inode": int(parent_info.st_ino),
        }
    except OSError:
        parent_filesystem = None
    return {
        "bootstrap": "harness-settings-home-v1",
        "lexical_path_hash": _sha256(str(lexical)),
        "parent_filesystem": parent_filesystem,
        "name": lexical.name,
    }


def _current_user_principal() -> str | int:
    """返回不能由普通环境变量伪造的本地 user principal。"""
    if os.name != "nt":
        get_uid = getattr(os, "getuid", None)
        if callable(get_uid):
            return int(get_uid())
        raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="scope")
    try:
        import ctypes

        advapi = ctypes.WinDLL("Advapi32.dll")
        kernel = ctypes.WinDLL("Kernel32.dll")
        token = ctypes.c_void_p()
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(),
            0x0008,  # TOKEN_QUERY
            ctypes.byref(token),
        ):
            raise OSError("OpenProcessToken failed")
        try:
            size = ctypes.c_uint32()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            if size.value == 0:
                raise OSError("GetTokenInformation failed")
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(
                token,
                1,
                buffer,
                size.value,
                ctypes.byref(size),
            ):
                raise OSError("GetTokenInformation failed")
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
            sid_text = ctypes.c_wchar_p()
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
                raise OSError("ConvertSidToStringSidW failed")
            try:
                return str(sid_text.value)
            finally:
                kernel.LocalFree(sid_text)
        finally:
            kernel.CloseHandle(token)
    except Exception as exc:
        raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="scope") from exc


def _sha256(value: object) -> str:
    """计算 canonical JSON SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _expected_account_for_record(record: DurableSettingRecord) -> str:
    """从 durable record 的 immutable identity 派生精确 credential account。"""
    return "harness-settings-v1-" + _sha256(
        {
            "scope_binding_digest": record.scope_binding_digest,
            "setting_id": record.setting_id,
            "generation": record.generation,
        }
    )


def _require_regular_file(path: Path, field_name: str) -> None:
    """拒绝 symlink、socket、FIFO、device 与 hardlink 多链接文件。"""
    try:
        info = path.lstat()
    except OSError as exc:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field=field_name) from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field=field_name)
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field=field_name)


def _windows_mutex_api() -> dict[str, Callable[..., object]]:
    """延迟加载 Windows named mutex API；加载失败即让 metadata 操作 fail closed。"""
    try:
        import ctypes

        kernel = ctypes.WinDLL("Kernel32.dll")
        create_raw = kernel.CreateMutexW
        create_raw.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_raw.restype = ctypes.c_void_p
        wait_raw = kernel.WaitForSingleObject
        wait_raw.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_raw.restype = ctypes.c_uint32
        release = kernel.ReleaseMutex
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_bool
        close = kernel.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_bool

        def create(name: str) -> object:
            """把 CreateMutexW 的三参数 ABI 收敛为 lock seam 的 name 参数。"""
            return create_raw(None, False, name)

        def wait(handle: object) -> object:
            """无限等待 mutex；超时不能让 metadata 写入继续。"""
            return wait_raw(handle, 0xFFFFFFFF)

        return {
            "create": create,
            "wait": wait,
            "release": release,
            "close": close,
        }
    except Exception as exc:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock", retryable=True) from exc


@contextmanager
def _windows_named_mutex_lock(
    path: Path,
    *,
    api: object | None = None,
) -> Iterator[None]:
    """使用不含原始路径的进程间命名 mutex，覆盖 Windows 无 fcntl 的并发场景。"""
    mutex_api = api or _windows_mutex_api()
    name = "Local\\za38-settings-" + hashlib.sha256(
        str(path.absolute()).encode("utf-8")
    ).hexdigest()

    def call(method: str, *args: object) -> object:
        target = mutex_api[method] if isinstance(mutex_api, Mapping) else getattr(mutex_api, method)
        return target(*args)  # type: ignore[operator]

    handle = call("create", name)
    if not handle:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock", retryable=True)
    acquired = False
    try:
        # WAIT_OBJECT_0 / WAIT_ABANDONED_0 均取得 mutex；WAIT_FAILED 等其它结果
        # 不能继续写 metadata。
        result = int(call("wait", handle))
        if result not in {0, 0x80}:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock", retryable=True)
        acquired = True
        yield
    finally:
        if acquired:
            call("release", handle)
        call("close", handle)


@contextmanager
def _file_lock(path: Path, *, root: Path) -> Iterator[None]:
    """在权限受限目录创建并持有 scope lock。"""
    _ensure_secure_directory(path.parent, root=root, create=True)
    try:
        if path.is_symlink():
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock")
        if path.exists():
            _require_regular_file(path, "lock")
        path.touch(mode=0o600, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o600)
        _require_regular_file(path, "lock")
        if os.name == "nt":
            # Windows 没有 POSIX mode；每个 scope lock 都检查实际 lock file
            # 的 owner-only DACL，不能只检查一个可能具有不同继承权限的 probe。
            acl_root = root / ".harness" / "settings" / "v1"
            acl_probe = WindowsCredentialBackend(metadata_root=acl_root)
            if not acl_probe._probe_metadata_acl(target=path):  # noqa: SLF001 - same security boundary
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="metadata_acl")
    except SettingsError:
        raise
    except OSError as exc:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock", retryable=True) from exc
    handle = path.open("r+b")
    try:
        if os.name == "nt":
            with _windows_named_mutex_lock(path):
                yield
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock", retryable=True)
    except OSError as exc:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="lock", retryable=True) from exc
    finally:
        handle.close()


def _require_windows_metadata_acl(path: Path, *, root: Path) -> None:
    """在 Windows 每次读写前证明实际 metadata 对象的 owner-only DACL。"""
    if os.name != "nt":
        return
    backend = WindowsCredentialBackend(metadata_root=root / ".harness" / "settings" / "v1")
    if not backend._probe_metadata_acl(target=path):  # noqa: SLF001 - same security boundary
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="metadata_acl")


def _atomic_json_write(path: Path, value: Mapping[str, object], *, root: Path) -> None:
    """在权限受限目录完成 temp fsync → atomic rename → dir fsync。"""
    _ensure_secure_directory(path.parent, root=root, create=True)
    if os.name == "nt":
        # 替换已有文件前先检查原对象；新文件则先检查继承 ACL 的父目录。
        # 原子替换后再次检查目标，避免把不可信 ACL 的旧对象当作安全写入。
        _require_windows_metadata_acl(path.parent, root=root)
        if path.exists():
            _require_windows_metadata_acl(path, root=root)
    try:
        path.parent.chmod(0o700)
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if path.exists() and os.name != "nt":
            path.chmod(0o600)
        if os.name == "nt":
            _require_windows_metadata_acl(path, root=root)
    except OSError as exc:
        try:
            if "temporary" in locals() and temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage", retryable=True) from exc


def _ensure_secure_directory(path: Path, *, root: Path, create: bool) -> bool:
    """校验 Settings 目录链，并在写入路径缺失时只创建受控子目录。

    ``Path.mkdir(parents=True)`` 会跟随中间 symlink；Settings metadata 的 scope
    和 journal 目录不能依赖这种默认行为。这里从受信任的 Harness home 开始，
    逐级 lstat，拒绝 symlink、非目录和对组/其他用户开放的目录。
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage") from exc
    if not root.exists():
        if not create:
            return False
        try:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage", retryable=True) from exc
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage")

    # ~/.harness 还承载 Plugin registry/store，既有目录通常不是
    # Settings 的私有边界；从 .harness/settings 开始才要求 0700。
    secure_boundary = root / ".harness" / "settings"
    current = root
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                info = current.lstat()
            except OSError as exc:
                raise SettingsError(
                    "SETTINGS_STORAGE_UNAVAILABLE",
                    field="storage",
                    retryable=True,
                ) from exc
            else:
                info = current.lstat()
        except OSError as exc:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage")
        try:
            # 保护从 .harness/settings 开始的完整私有链；共享的
            # .harness 目录由其他 Harness 数据复用，不在这里改变其权限。
            is_settings_directory = (
                current == secure_boundary
                or secure_boundary in current.parents
            )
        except (OSError, ValueError):
            is_settings_directory = False
        if is_settings_directory and os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            if not create:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage")
            try:
                current.chmod(0o700)
            except OSError as exc:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="storage") from exc
    return True


def _tombstone_from_dict(value: object, *, scope: Scope, binding: str) -> DurableTombstone:
    """严格解析 tombstone。"""
    expected = {
        "setting_id",
        "plugin_id",
        "package_digest",
        "declaration_digest",
        "scope",
        "scope_binding_digest",
        "tombstone_generation",
        "committed_revision",
    }
    if not isinstance(value, Mapping) or set(value) != expected or value.get("scope") != scope or value.get("scope_binding_digest") != binding:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="tombstones")
    revision = value.get("committed_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="tombstones")
    for field_name in ("setting_id", "plugin_id", "tombstone_generation"):
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="tombstones")
    if _SAFE_SETTING_ID_RE.fullmatch(str(value["setting_id"])) is None:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="tombstones")
    _validate_generation(value["tombstone_generation"], "tombstone_generation")
    _validate_digest(str(value["package_digest"]), "package_digest")
    _validate_digest(str(value["declaration_digest"]), "declaration_digest")
    return DurableTombstone(
        setting_id=str(value["setting_id"]),
        plugin_id=str(value["plugin_id"]),
        package_digest=str(value["package_digest"]),
        declaration_digest=str(value["declaration_digest"]),
        scope=scope,
        scope_binding_digest=binding,
        tombstone_generation=str(value["tombstone_generation"]),
        committed_revision=revision,
    )


def _validate_journal_payload(
    value: object,
    *,
    scope: Scope,
    binding: str,
) -> dict[str, object]:
    """按 operation 解析 journal closed union，拒绝跨操作字段和非法 phase。"""
    if not isinstance(value, Mapping):
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    operation = value.get("operation")
    common = {
        "schema_version",
        "operation_id",
        "operation",
        "phase",
        "scope",
        "scope_binding_digest",
        "retryable",
    }
    operation_fields: dict[str, set[str]] = {
        "set": {
            "record",
            "old_record",
            "old_generation",
            "new_generation",
            "new_account",
            "old_account",
        },
        "remove": {
            "record",
            "old_generation",
            "old_account",
            "tombstone",
        },
    }
    fields = operation_fields.get(operation) if isinstance(operation, str) else None
    if fields is None or set(value) != common | fields:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    if value.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str) or _SAFE_OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    if value.get("scope") != scope or value.get("scope_binding_digest") != binding:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    if not isinstance(value.get("retryable"), bool):
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    phase = value.get("phase")
    allowed_phases = {
        "set": {"prepared", "credential_written", "metadata_committed", "cleanup_pending"},
        "remove": {"prepared", "tombstone_committed", "credential_cleanup_pending"},
    }
    if phase not in allowed_phases[operation]:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")

    record = DurableSettingRecord.from_dict(
        value.get("record"),
        index_scope=scope,
        index_binding=binding,
    )
    if operation == "set":
        old_record_raw = value.get("old_record")
        old_record = None
        if old_record_raw is not None:
            old_record = DurableSettingRecord.from_dict(
                old_record_raw,
                index_scope=scope,
                index_binding=binding,
            )
            if old_record.plugin_id != record.plugin_id or old_record.env_var != record.env_var:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        old_generation = value.get("old_generation")
        if old_record is None:
            if old_generation is not None or value.get("old_account") is not None:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        else:
            if old_generation != old_record.generation:
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
            if _validate_account(value.get("old_account"), "old_account") != _expected_account_for_record(old_record):
                raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        if value.get("new_generation") != record.generation:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        if _validate_account(value.get("new_account"), "new_account") != _expected_account_for_record(record):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    else:
        if value.get("old_generation") != record.generation:
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        if _validate_account(value.get("old_account"), "old_account") != _expected_account_for_record(record):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
        tombstone = _tombstone_from_dict(value.get("tombstone"), scope=scope, binding=binding)
        if (
            tombstone.setting_id != record.setting_id
            or tombstone.plugin_id != record.plugin_id
            or tombstone.package_digest != record.package_digest
            or tombstone.declaration_digest != record.declaration_digest
        ):
            raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal")
    return dict(value)


def _journal_ref_from_dict(value: object) -> JournalRef:
    """严格解析安全 journal locator。"""
    if not isinstance(value, Mapping) or set(value) != {"operation_id", "file"}:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal_refs")
    operation_id = value.get("operation_id")
    file_name = value.get("file")
    if not isinstance(operation_id, str) or not _SAFE_OPERATION_ID_RE.fullmatch(operation_id):
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal_refs")
    if file_name != f"journal/{operation_id}.json":
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="journal_refs")
    return JournalRef(operation_id, file_name)


def _workspace_entry_from_dict(value: object) -> WorkspaceRegistryEntry:
    """严格解析 digest-only workspace registry entry。"""
    expected = {"scope_binding_digest", "metadata_locator", "state", "registered_revision"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
    digest = value.get("scope_binding_digest")
    locator = value.get("metadata_locator")
    state = value.get("state")
    revision = value.get("registered_revision")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
    if locator != f"workspaces/{digest}/index.json":
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
    if state not in {"registering", "registered", "removal_pending", "partial", "removed"}:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise SettingsError("SETTINGS_STORAGE_UNAVAILABLE", field="workspace_registry")
    return WorkspaceRegistryEntry(digest, locator, state, revision)


__all__ = [
    "CredentialBackend",
    "DurableSettingRecord",
    "DurableTombstone",
    "FakeCredentialBackend",
    "JournalRef",
    "LinuxSecretServiceCredentialBackend",
    "MAX_SETTING_VALUE_BYTES",
    "MacOSCredentialBackend",
    "MetadataIndexV1",
    "PendingSummary",
    "QwenSettingDeclaration",
    "SettingBinding",
    "SettingSummary",
    "SettingsError",
    "SettingsResolver",
    "SettingsSnapshot",
    "SettingsStore",
    "SimulatedSettingsCrash",
    "UnavailableCredentialBackend",
    "WorkspaceRegistryEntry",
    "WindowsCredentialBackend",
    "create_platform_credential_backend",
    "parse_qwen_setting",
    "parse_qwen_settings",
    "qwen_declaration_digest",
    "read_secret_stdin",
    "scope_binding_digest",
    "setting_identity",
    "validate_setting_value",
]
