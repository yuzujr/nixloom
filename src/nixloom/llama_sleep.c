#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <malloc.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * Zero-offset LD_PRELOAD hook for llama-server native sleep + KV/recurrent cache reuse.
 *
 * Instead of relying on private struct offsets inside server_context_impl or server_slot,
 * this library hooks exported C++ symbols:
 *   - server_slot::prompt_save(server_prompt_cache&) const
 *   - server_prompt_cache::load(...)
 *   - std::__cxx11::_List_base<server_prompt_cache_state, ...>::_M_clear()
 *   - server_context_impl::load_model(common_params&)
 *   - ggml_time_ms() / mtmd_free()
 */

struct cxx11_list_head {
    struct cxx11_list_head *next;
    struct cxx11_list_head *prev;
    size_t size;
};

static struct cxx11_list_head g_stashed_cache = {
    &g_stashed_cache,
    &g_stashed_cache,
    0,
};

static volatile sig_atomic_t want_sleep = 0;
static volatile sig_atomic_t slots_saved = 0;
static int64_t sleep_step = 0;
static int g_in_load_model = 0;

static const void *g_last_slot = NULL;
static void *g_current_cache = NULL;

typedef bool (*prompt_save_fn)(const void *, void *);
typedef void (*list_clear_fn)(void *);

static void list_init_empty(struct cxx11_list_head *h) {
    h->next = h;
    h->prev = h;
    h->size = 0;
}

static void list_move_nodes(struct cxx11_list_head *dst, struct cxx11_list_head *src) {
    if (src->next == src) {
        return;
    }
    dst->next = src->next;
    dst->prev = src->prev;
    dst->size = src->size;
    dst->next->prev = dst;
    dst->prev->next = dst;
    list_init_empty(src);
}

static void clear_prompt_cache_list(void *prompt_cache) {
    if (!prompt_cache) {
        return;
    }
    static list_clear_fn real_list_clear = NULL;
    if (!real_list_clear) {
        real_list_clear = (list_clear_fn)dlsym(
            RTLD_NEXT,
            "_ZNSt7__cxx1110_List_baseI25server_prompt_cache_stateSaIS1_EE8_M_clearEv"
        );
    }
    if (real_list_clear) {
        real_list_clear(prompt_cache);
        list_init_empty((struct cxx11_list_head *)prompt_cache);
        malloc_trim(0);
    }
}

void _ZNSt7__cxx1110_List_baseI25server_prompt_cache_stateSaIS1_EE8_M_clearEv(void *self) {
    static list_clear_fn real_list_clear = NULL;
    if (!real_list_clear) {
        real_list_clear = (list_clear_fn)dlsym(
            RTLD_NEXT,
            "_ZNSt7__cxx1110_List_baseI25server_prompt_cache_stateSaIS1_EE8_M_clearEv"
        );
    }
    struct cxx11_list_head *h = (struct cxx11_list_head *)self;
    if (g_in_load_model && h && h->next != h) {
        if (g_stashed_cache.next != &g_stashed_cache && real_list_clear) {
            real_list_clear(&g_stashed_cache);
            list_init_empty(&g_stashed_cache);
        }
        list_move_nodes(&g_stashed_cache, h);
        return;
    }
    if (real_list_clear) {
        real_list_clear(self);
    }
}

bool _ZNK11server_slot11prompt_saveER19server_prompt_cache(const void *slot, void *prompt_cache) {
    static prompt_save_fn real_prompt_save = NULL;
    if (!real_prompt_save) {
        real_prompt_save = (prompt_save_fn)dlsym(
            RTLD_NEXT,
            "_ZNK11server_slot11prompt_saveER19server_prompt_cache"
        );
    }
    g_last_slot = slot;
    g_current_cache = prompt_cache;
    return real_prompt_save(slot, prompt_cache);
}

static void save_slots_to_prompt_cache(void) {
    if (!g_last_slot || !g_current_cache) {
        return;
    }
    clear_prompt_cache_list(g_current_cache);
    bool saved = _ZNK11server_slot11prompt_saveER19server_prompt_cache(g_last_slot, g_current_cache);
    fprintf(stderr, "[nixloom-sleep] saved slot to prompt_cache (saved=%d)\n", (int)saved);
}

static void write_state(const char *state) {
    const char *path = getenv("NIXLOOM_SLEEP_STATE_FILE");
    if (!path || !*path) {
        return;
    }
    char tmp[1024];
    int n = snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    if (n < 0 || (size_t)n >= sizeof(tmp)) {
        return;
    }
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) {
        return;
    }
    char buf[128];
    int len = snprintf(buf, sizeof(buf), "%d %s\n", (int)getpid(), state);
    if (len > 0) {
        ssize_t written = write(fd, buf, (size_t)len);
        (void)written;
    }
    close(fd);
    (void)rename(tmp, path);
}

static void on_sigusr1(int sig) {
    (void)sig;
    slots_saved = 0;
    want_sleep = 1;
}

__attribute__((constructor))
static void nixloom_sleep_init(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_sigusr1;
    sa.sa_flags = SA_RESTART;
    sigaction(SIGUSR1, &sa, NULL);
}

int64_t ggml_time_ms(void) {
    static int64_t (*real_ggml_time_ms)(void) = NULL;
    if (!real_ggml_time_ms) {
        real_ggml_time_ms = dlsym(RTLD_NEXT, "ggml_time_ms");
    }
    int64_t now = real_ggml_time_ms();
    if (want_sleep) {
        if (!slots_saved) {
            slots_saved = 1;
            save_slots_to_prompt_cache();
        }
        sleep_step += 40000000000LL;
        return now + sleep_step;
    }
    return now;
}

void mtmd_free(void *ctx) {
    static void (*real_mtmd_free)(void *) = NULL;
    if (!real_mtmd_free) {
        real_mtmd_free = dlsym(RTLD_NEXT, "mtmd_free");
    }
    real_mtmd_free(ctx);
    g_last_slot = NULL;
    g_current_cache = NULL;
    want_sleep = 0;
    sleep_step = 0;
    malloc_trim(0);
    write_state("sleeping");
}

bool _ZN19server_context_impl10load_modelER13common_params(void *self, void *params) {
    static bool (*real_load_model)(void *, void *) = NULL;
    if (!real_load_model) {
        real_load_model = dlsym(RTLD_NEXT, "_ZN19server_context_impl10load_modelER13common_params");
    }
    g_last_slot = NULL;
    g_current_cache = NULL;
    g_in_load_model = 1;
    bool ok = real_load_model(self, params);
    g_in_load_model = 0;

    if (ok) {
        want_sleep = 0;
        slots_saved = 0;
        sleep_step = 0;
        write_state("awake");
    }
    return ok;
}

bool _ZN19server_prompt_cache4loadER13server_promptRK13server_tokensP13llama_contextS6_i(
    void *self,
    void *prompt,
    const void *tokens,
    void *ctx,
    void *ctx_dft,
    int slot_id
) {
    static bool (*real_load)(void *, void *, const void *, void *, void *, int) = NULL;
    if (!real_load) {
        real_load = dlsym(
            RTLD_NEXT,
            "_ZN19server_prompt_cache4loadER13server_promptRK13server_tokensP13llama_contextS6_i"
        );
    }
    g_current_cache = self;
    if (g_stashed_cache.next != &g_stashed_cache) {
        clear_prompt_cache_list(self);
        list_move_nodes((struct cxx11_list_head *)self, &g_stashed_cache);
    }
    bool ok = real_load(self, prompt, tokens, ctx, ctx_dft, slot_id);
    fprintf(stderr, "[nixloom-sleep] prompt_cache::load finished (ok=%d), releasing RAM cache\n", (int)ok);
    clear_prompt_cache_list(self);
    return ok;
}
