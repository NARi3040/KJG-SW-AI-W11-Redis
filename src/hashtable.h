#ifndef MINI_REDIS_HASHTABLE_H
#define MINI_REDIS_HASHTABLE_H

#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
#  ifdef HT_BUILD_DLL
#    define HT_API __declspec(dllexport)
#  else
#    define HT_API
#  endif
#  define HT_CALL __cdecl
#else
#  define HT_API
#  define HT_CALL
#endif

#define HT_OK 0
#define HT_NOT_FOUND 1
#define HT_NOMEM 2
#define HT_INVALID 4

typedef struct HtTable HtTable;
typedef struct {
  unsigned char *data;
  size_t length;
  int has_expiry;
  double expires_at;
} HtValue;
typedef struct {
  unsigned char *key;
  size_t key_length;
  HtValue value;
} HtEntry;
typedef struct {
  HtEntry *entries;
  size_t count;
} HtSnapshot;

HT_API int HT_CALL ht_create(HtTable **out_table);
HT_API void HT_CALL ht_destroy(HtTable *table);
HT_API int HT_CALL ht_put(HtTable *table, const unsigned char *key, size_t key_length,
                          const unsigned char *value, size_t value_length,
                          int has_expiry, double expires_at);
HT_API int HT_CALL ht_is_live(HtTable *table, const unsigned char *key, size_t key_length,
                              double cutoff, int *out_live);
HT_API int HT_CALL ht_get_copy(HtTable *table, const unsigned char *key, size_t key_length,
                               HtValue *out_value);
HT_API int HT_CALL ht_remove(HtTable *table, const unsigned char *key, size_t key_length,
                             int *out_removed);
HT_API int HT_CALL ht_remove_expired(HtTable *table, double cutoff);
HT_API int HT_CALL ht_copy_live(HtTable *table, double cutoff, HtSnapshot *out_snapshot);
HT_API void HT_CALL ht_free_value(HtValue *value);
HT_API void HT_CALL ht_free_snapshot(HtSnapshot *snapshot);

#if MINI_REDIS_TESTING
/* MINI_REDIS_TESTING=1인 시험 빌드에서만 제공 */
HT_API void HT_CALL ht_test_fail_alloc_after(int64_t successful_allocations);
HT_API int HT_CALL ht_test_dump(HtTable *table, HtSnapshot *out_snapshot,
                                size_t *out_count, size_t *out_bucket_count);
#endif

#endif
