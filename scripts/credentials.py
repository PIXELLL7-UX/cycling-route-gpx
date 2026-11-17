"""Store credentials outside the Skill, using the operating system credential store."""

import ctypes
import getpass
import sys


class CredentialError(Exception):
    pass


def _keyring():
    try:
        import keyring
    except ImportError:
        raise CredentialError("当前系统需要安装 Python keyring 并配置系统凭证库，或使用 Agent 的持久密钥配置。") from None
    # Do not silently use third-party plaintext/file backends.
    backend = keyring.get_keyring()
    if type(backend).__module__ not in {
        "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet",
    }:
        raise CredentialError("未检测到受支持的系统凭证库；请配置系统 keyring 或 Agent 的持久密钥管理。")
    return keyring


def read_other_platform(provider):
    try:
        return _keyring().get_password(f"cycling-route-gpx-{provider}", getpass.getuser()) or ""
    except CredentialError:
        raise
    except Exception:
        raise CredentialError("无法读取系统凭证库，请解锁或授权访问；无需重新申请密钥。") from None


def read_macos(provider):
    """Use the same Python/Security caller as writes, rather than security CLI ACLs."""
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr, u32 = ctypes.c_void_p, ctypes.c_uint32
        find = security.SecKeychainFindGenericPassword
        find.argtypes = [ptr, u32, ctypes.c_char_p, u32, ctypes.c_char_p,
                         ctypes.POINTER(u32), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
        find.restype = ctypes.c_int32
        release = security.SecKeychainItemFreeContent
        release.argtypes = [ptr, ptr]
        release.restype = ctypes.c_int32
        service = f"cycling-route-gpx-{provider}".encode()
        account = getpass.getuser().encode()
        length, data = u32(), ptr()
        status = find(None, len(service), service, len(account), account,
                      ctypes.byref(length), ctypes.byref(data), None)
        if status == -25300:
            return ""
        if status:
            raise CredentialError("无法读取 macOS 钥匙串，请解锁或授权 Python 访问；无需重新申请密钥。")
        try:
            return ctypes.string_at(data, length.value).decode().strip()
        finally:
            release(None, data)
    except (OSError, AttributeError, UnicodeError):
        raise CredentialError("无法读取 macOS 钥匙串，请检查系统权限；无需重新申请密钥。") from None


def save_key(provider, key):
    if provider not in {"amap", "baidu"} or not key.strip():
        raise CredentialError("地图类型或密钥为空，未保存。")
    service = f"cycling-route-gpx-{provider}"
    account = getpass.getuser()
    if sys.platform != "darwin":
        try:
            _keyring().set_password(service, account, key)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("系统凭证库保存失败，请检查是否已解锁或配置 Agent 的持久密钥管理。") from None
        return
    # Call Security.framework directly: no password in argv, files, or shell history.
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ptr, u32 = ctypes.c_void_p, ctypes.c_uint32
        find = security.SecKeychainFindGenericPassword
        find.argtypes = [ptr, u32, ctypes.c_char_p, u32, ctypes.c_char_p,
                         ctypes.POINTER(u32), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
        find.restype = ctypes.c_int32
        add = security.SecKeychainAddGenericPassword
        add.argtypes = [ptr, u32, ctypes.c_char_p, u32, ctypes.c_char_p,
                        u32, ptr, ctypes.POINTER(ptr)]
        add.restype = ctypes.c_int32
        update = security.SecKeychainItemModifyAttributesAndData
        update.argtypes = [ptr, ptr, u32, ptr]
        update.restype = ctypes.c_int32
        core.CFRelease.argtypes = [ptr]
        core.CFRelease.restype = None
        service_bytes, account_bytes, secret = service.encode(), account.encode(), key.encode()
        item = ptr()
        status = find(None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                      None, None, ctypes.byref(item))
        try:
            if status == 0:
                status = update(item, None, len(secret), secret)
            elif status == -25300:  # errSecItemNotFound
                status = add(None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
                             len(secret), secret, None)
            if status == 100001:
                raise CredentialError("当前执行环境不允许写入 macOS 钥匙串；请在系统终端运行 configure-key 命令完成保存。")
            if status != 0:
                raise CredentialError(f"macOS 钥匙串保存失败（状态 {status}），请检查钥匙串是否解锁及执行环境权限。")
        finally:
            if item.value:
                core.CFRelease(item)
    except (OSError, AttributeError):
        raise CredentialError("无法访问 macOS 系统钥匙串，密钥未保存。") from None


if __name__ == "__main__":
    # Private subprocess protocol: stdout is captured by route_gpx, never displayed.
    if len(sys.argv) != 2 or sys.argv[1] not in {"amap", "baidu"}:
        sys.exit(2)
    try:
        secret = read_macos(sys.argv[1])
        if not secret:
            sys.exit(44)
        sys.stdout.write(secret)
    except CredentialError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
