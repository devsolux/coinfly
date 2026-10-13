// Coinfly 1.0.0 / com.devsolux.coinfly. Native Security Framework bridge.
// Build only in the signed macOS ARM64 release pipeline. Empty team pin denies use.
#ifndef __STDC_WANT_LIB_EXT1__
#define __STDC_WANT_LIB_EXT1__ 1
#endif
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <cstring>
#include <cstddef>

#ifndef COINFLY_TEAM_ID
#define COINFLY_TEAM_ID ""
#endif

static constexpr size_t kMaximumSecret = 2400;
static constexpr size_t kMetadataLength = 32;
static NSString *const kService = @"com.devsolux.coinfly";

static bool validBinding(const char *binding) {
    if (!binding || std::strlen(binding) != 32) return false;
    for (size_t i = 0; i < 32; ++i)
        if (!((binding[i] >= 'a' && binding[i] <= 'f') || (binding[i] >= '0' && binding[i] <= '9'))) return false;
    return true;
}

static bool sameDigest(const unsigned char *left, const unsigned char *right) {
    unsigned char mismatch = 0;
    for (size_t i = 0; i < kMetadataLength; ++i) mismatch |= left[i] ^ right[i];
    return mismatch == 0;
}

extern "C" __attribute__((visibility("default"))) int coinfly_vault_check_identity() {
    if (std::strlen(COINFLY_TEAM_ID) == 0) return -1;
    @autoreleasepool {
        NSString *requirementText = [NSString stringWithFormat:
            @"anchor apple generic and identifier \"com.devsolux.coinfly.worker\" and certificate leaf[subject.OU] = \"%s\"",
            COINFLY_TEAM_ID];
        SecRequirementRef requirement = nullptr;
        SecCodeRef code = nullptr;
        OSStatus status = SecRequirementCreateWithString((__bridge CFStringRef)requirementText, kSecCSDefaultFlags, &requirement);
        if (status == errSecSuccess) status = SecCodeCopySelf(kSecCSDefaultFlags, &code);
        if (status == errSecSuccess) status = SecCodeCheckValidity(code, kSecCSStrictValidate, requirement);
        if (code) CFRelease(code);
        if (requirement) CFRelease(requirement);
        return status == errSecSuccess ? 0 : -1;
    }
}

static NSMutableDictionary *queryFor(const char *binding) {
    return [@{
        (__bridge id)kSecClass: (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService: kService,
        (__bridge id)kSecAttrAccount: [NSString stringWithUTF8String:binding],
        (__bridge id)kSecUseDataProtectionKeychain: @YES,
        (__bridge id)kSecAttrSynchronizable: @NO,
        (__bridge id)kSecUseAuthenticationUI: (__bridge id)kSecUseAuthenticationUIFail
    } mutableCopy];
}

extern "C" __attribute__((visibility("default"))) int coinfly_vault_put(
    const char *binding, const unsigned char *metadata, const unsigned char *secret, size_t length) {
    if (coinfly_vault_check_identity() || !validBinding(binding) || !metadata || !secret || length == 0 || length > kMaximumSecret) return -1;
    @autoreleasepool {
        NSMutableDictionary *query = queryFor(binding);
        NSMutableData *payload = [NSMutableData dataWithLength:kMetadataLength + length];
        std::memcpy(payload.mutableBytes, metadata, kMetadataLength);
        std::memcpy((unsigned char *)payload.mutableBytes + kMetadataLength, secret, length);
        query[(__bridge id)kSecAttrAccessible] = (__bridge id)kSecAttrAccessibleWhenUnlockedThisDeviceOnly;
        query[(__bridge id)kSecAttrGeneric] = [NSData dataWithBytes:metadata length:kMetadataLength];
        query[(__bridge id)kSecValueData] = payload;
        // Never update an existing binding in place: enroll a fresh version/ID.
        OSStatus status = SecItemAdd((__bridge CFDictionaryRef)query, nullptr);
        // Darwin exposes memset_s; unlike memset, this clearing cannot be
        // optimized away after the last use of the credential payload.
        const int cleared = memset_s(payload.mutableBytes, payload.length, 0, payload.length);
        if (cleared != 0) return -1;
        return status == errSecSuccess ? 0 : -1;
    }
}

extern "C" __attribute__((visibility("default"))) int coinfly_vault_read(
    const char *binding, const unsigned char *metadata, unsigned char *output, size_t *length) {
    if (coinfly_vault_check_identity() || !validBinding(binding) || !metadata || !output || !length) return -1;
    @autoreleasepool {
        NSMutableDictionary *query = queryFor(binding);
        query[(__bridge id)kSecReturnData] = @YES;
        query[(__bridge id)kSecMatchLimit] = (__bridge id)kSecMatchLimitOne;
        CFTypeRef result = nullptr;
        OSStatus status = SecItemCopyMatching((__bridge CFDictionaryRef)query, &result);
        if (status != errSecSuccess || !result) { if (result) CFRelease(result); return -1; }
        if (CFGetTypeID(result) != CFDataGetTypeID()) { CFRelease(result); return -1; }
        CFDataRef data = (CFDataRef)result;
        CFIndex size = CFDataGetLength(data);
        const UInt8 *bytes = CFDataGetBytePtr(data);
        int outcome = -1;
        if (size > (CFIndex)kMetadataLength && size <= (CFIndex)(kMetadataLength + kMaximumSecret)) {
            if (!sameDigest(bytes, metadata)) outcome = -2;
            else if (*length >= (size_t)size - kMetadataLength) {
                *length = (size_t)size - kMetadataLength;
                std::memcpy(output, bytes + kMetadataLength, *length);
                outcome = 0;
            }
        }
        CFRelease(result);
        return outcome;
    }
}

extern "C" __attribute__((visibility("default"))) int coinfly_vault_delete(
    const char *binding, const unsigned char *metadata) {
    if (coinfly_vault_check_identity() || !validBinding(binding) || !metadata) return -1;
    @autoreleasepool {
        NSMutableDictionary *query = queryFor(binding);
        query[(__bridge id)kSecAttrGeneric] = [NSData dataWithBytes:metadata length:kMetadataLength];
        OSStatus status = SecItemDelete((__bridge CFDictionaryRef)query);
        return (status == errSecSuccess || status == errSecItemNotFound) ? 0 : -1;
    }
}
