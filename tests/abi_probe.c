/* B2: C 구조체 크기와 오프셋을 "이름 값" 한 줄씩 출력한다. ctypes 선언과 대조한다. */
#include <stddef.h>
#include <stdio.h>

#include "hashtable.h"

#define SIZE(t) printf("sizeof(%s) %zu\n", #t, sizeof(t))
#define OFF(t, f) printf("offsetof(%s.%s) %zu\n", #t, #f, offsetof(t, f))

int main(void) {
  SIZE(HtValue);
  OFF(HtValue, data);
  OFF(HtValue, length);
  OFF(HtValue, has_expiry);
  OFF(HtValue, expires_at);
  SIZE(HtEntry);
  OFF(HtEntry, key);
  OFF(HtEntry, key_length);
  OFF(HtEntry, value);
  SIZE(HtSnapshot);
  OFF(HtSnapshot, entries);
  OFF(HtSnapshot, count);
  return 0;
}
