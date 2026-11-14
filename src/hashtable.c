#include "hashtable.h"

#include <math.h>
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
  size_t bucket_count;
  size_t count;
};

#if MINI_REDIS_TESTING
static int64_t fail_after = -1;

void ht_test_fail_alloc_after(int64_t successful_allocations) {
  fail_after = successful_allocations;
}
#endif

static void *allocate(size_t count, size_t size) {
#if MINI_REDIS_TESTING
  if (fail_after == 0) {
    fail_after = -1;
    return NULL;
  }
  if (fail_after > 0) --fail_after;
#endif
  return calloc(count, size);
}

static uint64_t hash(const unsigned char *key, size_t length) {
  uint64_t h = UINT64_C(5381);
  for (size_t i = 0; i < length; ++i) h = h * 33 + key[i];
  return h;
}

static int valid_key(HtTable *table, const unsigned char *key, size_t length) {
  return table != NULL && (length == 0 || key != NULL);
}

static HtNode **find(HtTable *table, const unsigned char *key, size_t length) {
  HtNode **link = &table->buckets[hash(key, length) % table->bucket_count];
  while (*link != NULL) {
    if ((*link)->key_length == length &&
        (length == 0 || memcmp((*link)->key, key, length) == 0)) break;
    link = &(*link)->next;
  }
  return link;
}

static int copy_bytes(const unsigned char *source, size_t length,
                      unsigned char **out) {
  *out = NULL;
  if (length == 0) return HT_OK;
  *out = allocate(length, 1);
  if (*out == NULL) return HT_NOMEM;
  memcpy(*out, source, length);
  return HT_OK;
}

void ht_free_value(HtValue *value) {
  if (value == NULL) return;
  free(value->data);
  memset(value, 0, sizeof(*value));
}

static void free_node(HtNode *node) {
  free(node->key);
  ht_free_value(&node->value);
  free(node);
}

static int copy_value(const HtValue *source, HtValue *out) {
  int status = copy_bytes(source->data, source->length, &out->data);
  if (status != HT_OK) return status;
  out->length = source->length;
  out->has_expiry = source->has_expiry;
  out->expires_at = source->expires_at;
  return HT_OK;
}

static int live(const HtNode *node, double cutoff) {
  return !node->value.has_expiry || node->value.expires_at > cutoff;
}

int ht_create(HtTable **out_table) {
  if (out_table == NULL) return HT_INVALID;
  *out_table = NULL;
  HtTable *table = allocate(1, sizeof(*table));
  if (table == NULL) return HT_NOMEM;
  table->bucket_count = 16;
  table->buckets = allocate(table->bucket_count, sizeof(*table->buckets));
  if (table->buckets == NULL) {
    free(table);
    return HT_NOMEM;
  }
  *out_table = table;
  return HT_OK;
}

void ht_destroy(HtTable *table) {
  if (table == NULL) return;
  for (size_t i = 0; i < table->bucket_count; ++i) {
    HtNode *node = table->buckets[i];
    while (node != NULL) {
      HtNode *next = node->next;
      free_node(node);
      node = next;
    }
  }
  free(table->buckets);
  free(table);
}

int ht_put(HtTable *table, const unsigned char *key, size_t key_length,
           const unsigned char *value, size_t value_length,
           int has_expiry, double expires_at) {
  if (!valid_key(table, key, key_length) || (value_length && value == NULL) ||
      (has_expiry != 0 && has_expiry != 1) ||
      (has_expiry && !isfinite(expires_at))) return HT_INVALID;
  HtNode **link = find(table, key, key_length);
  HtNode *old = *link;
  HtNode *node = allocate(1, sizeof(*node));
  if (node == NULL) return HT_NOMEM;
  if (copy_bytes(key, key_length, &node->key) != HT_OK ||
      copy_bytes(value, value_length, &node->value.data) != HT_OK) {
    free_node(node);
    return HT_NOMEM;
  }
  node->key_length = key_length;
  node->value.length = value_length;
  node->value.has_expiry = has_expiry;
  node->value.expires_at = has_expiry ? expires_at : 0;
  HtNode **buckets = NULL;
  size_t m = table->bucket_count;
  if (old == NULL && table->count >= m - m / 4) {
    if (m > SIZE_MAX / 2) {
      free_node(node);
      return HT_NOMEM;
    }
    buckets = allocate(m * 2, sizeof(*buckets));
    if (buckets == NULL) {
      free_node(node);
      return HT_NOMEM;
    }
  }
  /* All resources are ready: commit without allocations or callbacks. */
  if (old != NULL) {
    node->next = old->next;
    *link = node;
    free_node(old);
    return HT_OK;
  }
  if (buckets != NULL) {
    for (size_t i = 0; i < m; ++i) {
      HtNode *current = table->buckets[i];
      while (current != NULL) {
        HtNode *next = current->next;
        size_t index = hash(current->key, current->key_length) % (m * 2);
        current->next = buckets[index];
        buckets[index] = current;
        current = next;
      }
    }
    free(table->buckets);
    table->buckets = buckets;
    table->bucket_count = m * 2;
  }
  size_t index = hash(key, key_length) % table->bucket_count;
  node->next = table->buckets[index];
  table->buckets[index] = node;
  ++table->count;
  return HT_OK;
}

int ht_is_live(HtTable *table, const unsigned char *key, size_t key_length,
               double cutoff, int *out_live) {
  if (out_live != NULL) *out_live = 0;
  if (!valid_key(table, key, key_length) || out_live == NULL ||
      !isfinite(cutoff)) return HT_INVALID;
  HtNode *node = *find(table, key, key_length);
  *out_live = node != NULL && live(node, cutoff);
  return HT_OK;
}

int ht_get_copy(HtTable *table, const unsigned char *key, size_t key_length,
                HtValue *out_value) {
  if (out_value != NULL) memset(out_value, 0, sizeof(*out_value));
  if (!valid_key(table, key, key_length) || out_value == NULL) return HT_INVALID;
  HtNode *node = *find(table, key, key_length);
  return node == NULL ? HT_NOT_FOUND : copy_value(&node->value, out_value);
}

int ht_remove(HtTable *table, const unsigned char *key, size_t key_length,
              int *out_removed) {
  if (out_removed != NULL) *out_removed = 0;
  if (!valid_key(table, key, key_length) || out_removed == NULL) return HT_INVALID;
  HtNode **link = find(table, key, key_length);
  HtNode *node = *link;
  if (node != NULL) {
    *link = node->next;
    free_node(node);
    --table->count;
    *out_removed = 1;
  }
  return HT_OK;
}

int ht_remove_expired(HtTable *table, double cutoff) {
  if (table == NULL || !isfinite(cutoff)) return HT_INVALID;
  for (size_t i = 0; i < table->bucket_count; ++i) {
    HtNode **link = &table->buckets[i];
    while (*link != NULL) {
      HtNode *node = *link;
      if (!live(node, cutoff)) {
        *link = node->next;
        free_node(node);
        --table->count;
      } else {
        link = &node->next;
      }
    }
  }
  return HT_OK;
}

void ht_free_snapshot(HtSnapshot *snapshot) {
  if (snapshot == NULL) return;
  for (size_t i = 0; i < snapshot->count; ++i) {
    free(snapshot->entries[i].key);
    ht_free_value(&snapshot->entries[i].value);
  }
  free(snapshot->entries);
  memset(snapshot, 0, sizeof(*snapshot));
}

static int copy_entries(HtTable *table, double cutoff, int all, HtSnapshot *out) {
  size_t count = 0;
  for (size_t i = 0; i < table->bucket_count; ++i)
    for (HtNode *node = table->buckets[i]; node != NULL; node = node->next)
      if (all || live(node, cutoff)) ++count;
  if (count == 0) return HT_OK;
  HtSnapshot result = {0};
  result.entries = allocate(count, sizeof(*result.entries));
  if (result.entries == NULL) return HT_NOMEM;
  for (size_t i = 0; i < table->bucket_count; ++i) {
    for (HtNode *node = table->buckets[i]; node != NULL; node = node->next) {
      if (!all && !live(node, cutoff)) continue;
      HtEntry *entry = &result.entries[result.count++];
      if (copy_bytes(node->key, node->key_length, &entry->key) != HT_OK ||
          copy_value(&node->value, &entry->value) != HT_OK) {
        ht_free_snapshot(&result);
        return HT_NOMEM;
      }
      entry->key_length = node->key_length;
    }
  }
  *out = result;
  return HT_OK;
}

int ht_copy_live(HtTable *table, double cutoff, HtSnapshot *out_snapshot) {
  if (out_snapshot != NULL) memset(out_snapshot, 0, sizeof(*out_snapshot));
  if (table == NULL || out_snapshot == NULL || !isfinite(cutoff)) return HT_INVALID;
  return copy_entries(table, cutoff, 0, out_snapshot);
}

#if MINI_REDIS_TESTING
int ht_test_dump(HtTable *table, HtSnapshot *out_snapshot,
                 size_t *out_count, size_t *out_bucket_count) {
  if (out_snapshot != NULL) memset(out_snapshot, 0, sizeof(*out_snapshot));
  if (out_count != NULL) *out_count = 0;
  if (out_bucket_count != NULL) *out_bucket_count = 0;
  if (table == NULL || out_snapshot == NULL || out_count == NULL ||
      out_bucket_count == NULL) return HT_INVALID;
  int status = copy_entries(table, 0, 1, out_snapshot);
  if (status == HT_OK) {
    *out_count = table->count;
    *out_bucket_count = table->bucket_count;
  }
  return status;
}
#endif
