// Coinfly 1.0.0 / com.devsolux.coinfly. Current-user Credential Manager bridge.
// Build only in the signed Windows AMD64 release pipeline; empty signer pin denies use.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wincred.h>
#include <wintrust.h>
#include <softpub.h>
#include <wincrypt.h>
#include <string>
#include <vector>
#include <cstring>
#include <cstddef>

#pragma comment(lib, "Advapi32.lib")
#pragma comment(lib, "Wintrust.lib")
#pragma comment(lib, "Crypt32.lib")

#ifndef COINFLY_SIGNER_CERT_SHA256_HEX
#define COINFLY_SIGNER_CERT_SHA256_HEX ""
#endif

static constexpr size_t kMaximumSecret = 2400;
static constexpr size_t kMetadataLength = 32;

static bool validBinding(const char *binding) {
    if (!binding || std::strlen(binding) != 32) return false;
    for (size_t i = 0; i < 32; ++i)
        if (!((binding[i] >= 'a' && binding[i] <= 'f') || (binding[i] >= '0' && binding[i] <= '9'))) return false;
    return true;
}

static bool sameDigest(const BYTE *left, const BYTE *right) {
    BYTE mismatch = 0;
    for (size_t i = 0; i < kMetadataLength; ++i) mismatch |= left[i] ^ right[i];
    return mismatch == 0;
}

static std::wstring targetFor(const char *binding) {
    std::wstring target = L"com.devsolux.coinfly/";
    for (size_t i = 0; i < 32; ++i) target.push_back((wchar_t)binding[i]);
    return target;
}

extern "C" __declspec(dllexport) int coinfly_vault_check_identity() {
    const char *pin = COINFLY_SIGNER_CERT_SHA256_HEX;
    if (std::strlen(pin) != 64) return -1;
    wchar_t path[32768]{};
    DWORD length = GetModuleFileNameW(nullptr, path, 32768);
    if (length == 0 || length >= 32768) return -1;
    WINTRUST_FILE_INFO fileInfo{};
    fileInfo.cbStruct = sizeof(fileInfo);
    fileInfo.pcwszFilePath = path;
    WINTRUST_DATA trust{};
    trust.cbStruct = sizeof(trust);
    trust.dwUIChoice = WTD_UI_NONE;
    trust.fdwRevocationChecks = WTD_REVOKE_WHOLECHAIN;
    trust.dwUnionChoice = WTD_CHOICE_FILE;
    trust.pFile = &fileInfo;
    trust.dwStateAction = WTD_STATEACTION_VERIFY;
    trust.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT;
    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    LONG status = WinVerifyTrust(nullptr, &action, &trust);
    int result = -1;
    if (status == ERROR_SUCCESS) {
        CRYPT_PROVIDER_DATA *provider = WTHelperProvDataFromStateData(trust.hWVTStateData);
        CRYPT_PROVIDER_SGNR *signer = provider ? WTHelperGetProvSignerFromChain(provider, 0, FALSE, 0) : nullptr;
        CRYPT_PROVIDER_CERT *certificate = signer ? WTHelperGetProvCertFromChain(signer, 0) : nullptr;
        BYTE hash[32]{};
        DWORD hashLength = sizeof(hash);
        if (certificate && certificate->pCert && CertGetCertificateContextProperty(certificate->pCert, CERT_SHA256_HASH_PROP_ID, hash, &hashLength) && hashLength == sizeof(hash)) {
            static const char digits[] = "0123456789abcdef";
            BYTE difference = 0;
            for (size_t i = 0; i < sizeof(hash); ++i) {
                difference |= (BYTE)(digits[hash[i] >> 4] ^ pin[i * 2]);
                difference |= (BYTE)(digits[hash[i] & 15] ^ pin[i * 2 + 1]);
            }
            if (difference == 0) result = 0;
        }
        SecureZeroMemory(hash, sizeof(hash));
    }
    trust.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(nullptr, &action, &trust);
    return result;
}

extern "C" __declspec(dllexport) int coinfly_vault_put(
    const char *binding, const BYTE *metadata, const BYTE *secret, size_t length) {
    if (coinfly_vault_check_identity() || !validBinding(binding) || !metadata || !secret || !length || length > kMaximumSecret) return -1;
    std::wstring target = targetFor(binding);
    PCREDENTIALW existing = nullptr;
    if (CredReadW(target.c_str(), CRED_TYPE_GENERIC, 0, &existing)) {
        if (existing->CredentialBlob) SecureZeroMemory(existing->CredentialBlob, existing->CredentialBlobSize);
        CredFree(existing);
        return -1;  // New binding per enrollment/version; no in-place overwrite.
    }
    if (GetLastError() != ERROR_NOT_FOUND) return -1;
    std::vector<BYTE> payload(kMetadataLength + length);
    std::memcpy(payload.data(), metadata, kMetadataLength);
    std::memcpy(payload.data() + kMetadataLength, secret, length);
    CREDENTIAL_ATTRIBUTEW attribute{};
    attribute.Keyword = const_cast<LPWSTR>(L"coinfly.realm.sha256");
    attribute.ValueSize = kMetadataLength;
    attribute.Value = const_cast<LPBYTE>(metadata);
    CREDENTIALW credential{};
    credential.Type = CRED_TYPE_GENERIC;
    credential.TargetName = target.data();
    credential.CredentialBlobSize = (DWORD)payload.size();
    credential.CredentialBlob = payload.data();
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE; // Same current user, local machine, never roaming.
    credential.AttributeCount = 1;
    credential.Attributes = &attribute;
    BOOL ok = CredWriteW(&credential, 0);
    SecureZeroMemory(payload.data(), payload.size());
    return ok ? 0 : -1;
}

extern "C" __declspec(dllexport) int coinfly_vault_read(
    const char *binding, const BYTE *metadata, BYTE *output, size_t *length) {
    if (coinfly_vault_check_identity() || !validBinding(binding) || !metadata || !output || !length) return -1;
    std::wstring target = targetFor(binding);
    PCREDENTIALW credential = nullptr;
    if (!CredReadW(target.c_str(), CRED_TYPE_GENERIC, 0, &credential)) return -1;
    int result = -1;
    if (credential->Persist == CRED_PERSIST_LOCAL_MACHINE && credential->CredentialBlobSize > kMetadataLength && credential->CredentialBlobSize <= kMaximumSecret + kMetadataLength) {
        if (!sameDigest(credential->CredentialBlob, metadata)) result = -2;
        else if (*length >= credential->CredentialBlobSize - kMetadataLength) {
            *length = credential->CredentialBlobSize - kMetadataLength;
            std::memcpy(output, credential->CredentialBlob + kMetadataLength, *length);
            result = 0;
        }
    }
    if (credential->CredentialBlob) SecureZeroMemory(credential->CredentialBlob, credential->CredentialBlobSize);
    CredFree(credential);
    return result;
}

extern "C" __declspec(dllexport) int coinfly_vault_delete(const char *binding, const BYTE *metadata) {
    if (coinfly_vault_check_identity() || !validBinding(binding) || !metadata) return -1;
    BYTE secret[kMaximumSecret]{};
    size_t length = sizeof(secret);
    int verified = coinfly_vault_read(binding, metadata, secret, &length);
    SecureZeroMemory(secret, sizeof(secret));
    if (verified) return verified;
    std::wstring target = targetFor(binding);
    return CredDeleteW(target.c_str(), CRED_TYPE_GENERIC, 0) ? 0 : -1;
}
