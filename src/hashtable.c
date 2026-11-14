/* MR-00 뼈대: int 반환 함수는 출력을 비우고 HT_UNIMPLEMENTED, void 반환 함수는 abort()로 중단한다. */
#include "hashtable.h"

#include <stdlib.h>
#include <string.h>

typedef struct HtNode {
  unsigned char *key;
  size_t key_length;
  HtValue value;
  struct HtNode *next;
} HtNode;

struct HtTable {
  HtNode **buckets;
  size_t bucket_count; /* m */
  size_t count;        /* n: 만료 항목 포함 물리 항목 수 */
};

int ht_create(HtTable **out_table) {
  if (out_table != NULL) *out_table = NULL;
  return HT_UNIMPLEMENTED;
}

void ht_destroy(HtTable *table) {
  (void)table;
  abort();
}

int ht_put(HtTable *table, const unsigned char *key, size_t key_length,
           const unsigned char *value, size_t value_length,
           int has_expiry, double expires_at) {
  (void)table; (void)key; (void)key_length; (void)value; (void)value_length;
  (void)has_expiry; (void)expires_at;
  return HT_UNIMPLEMENTED;
}

int ht_is_live(HtTable *table, const unsigned char *key, size_t key_length,
               double cutoff, int *out_live) {
  (void)table; (void)key; (void)key_length; (void)cutoff;
  if (out_live != NULL) *out_live = 0;
  return HT_UNIMPLEMENTED;
}

int ht_get_copy(HtTable *table, const unsigned char *key, size_t key_length,
                HtValue *out_value) {
  (void)table; (void)key; (void)key_length;
  if (out_value != NULL) memset(out_value, 0, sizeof(*out_value));
  return HT_UNIMPLEMENTED;
}

int ht_remove(HtTable *table, const unsigned char *key, size_t key_length,
              int *out_removed) {
  (void)table; (void)key; (void)key_length;
  if (out_removed != NULL) *out_removed = 0;
  return HT_UNIMPLEMENTED;
}

int ht_remove_expired(HtTable *table, double cutoff) {
  (void)table; (void)cutoff;
  return HT_UNIMPLEMENTED;
}

int ht_copy_live(HtTable *table, double cutoff, HtSnapshot *out_snapshot) {
  (void)table; (void)cutoff;
  if (out_snapshot != NULL) memset(out_snapshot, 0, sizeof(*out_snapshot));
  return HT_UNIMPLEMENTED;
}

void ht_free_value(HtValue *value) {
  (void)value;
  abort();
}

void ht_free_snapshot(HtSnapshot *snapshot) {
  (void)snapshot;
  abort();
}

#if MINI_REDIS_TESTING
void ht_test_fail_alloc_after(int64_t successful_allocations) {
  (void)successful_allocations;
  abort();
}

int ht_test_dump(HtTable *table, HtSnapshot *out_snapshot,
                 size_t *out_count, size_t *out_bucket_count) {
  (void)table;
  if (out_snapshot != NULL) memset(out_snapshot, 0, sizeof(*out_snapshot));
  if (out_count != NULL) *out_count = 0;
  if (out_bucket_count != NULL) *out_bucket_count = 0;
  return HT_UNIMPLEMENTED;
}
#endif
