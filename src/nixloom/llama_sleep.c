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
typedef size_t (*cache_n_tokens_fn)(const void *);
typedef size_t (*tokens_common_prefix_fn)(const void *, const void *);

struct llama_tensor_buft_override_ {
    const char *pattern;
    void *buft;
};

struct llama_model_params_prefix {
    void *devices;
    const struct llama_tensor_buft_override_ *tensor_buft_overrides;
};

static void list_init_empty(struct cxx11_list_head *h) {
    h->next = h;
    h->prev = h;
    h->size = 0;
}

static void list_splice_back(struct cxx11_list_head *dst, struct cxx11_list_head *src) {
    if (src->next == src) {
        return;
    }
    struct cxx11_list_head *first = src->next;
    struct cxx11_list_head *last = src->prev;
    struct cxx11_list_head *at = dst->prev;
    at->next = first;
    first->prev = at;
    last->next = dst;
    dst->prev = last;
    dst->size += src->size;
    list_init_empty(src);
}

static size_t get_cache_n_tokens(const void *prompt_cache) {
    if (!prompt_cache) {
        return 0;
    }
    static cache_n_tokens_fn real_n_tokens = NULL;
    if (!real_n_tokens) {
        real_n_tokens = (cache_n_tokens_fn)dlsym(
            RTLD_NEXT,
            "_ZNK19server_prompt_cache8n_tokensEv"
        );
    }
    return real_n_tokens ? real_n_tokens(prompt_cache) : 0;
}

static size_t get_tokens_size(const void *tokens) {
    if (!tokens) {
        return 0;
    }
    static tokens_common_prefix_fn real_common_prefix = NULL;
    if (!real_common_prefix) {
        real_common_prefix = (tokens_common_prefix_fn)dlsym(
            RTLD_NEXT,
            "_ZNK13server_tokens17get_common_prefixERKS_"
        );
    }
    return real_common_prefix ? real_common_prefix(tokens, tokens) : 0;
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
        list_splice_back(&g_stashed_cache, h);
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
    bool saved = _ZNK11server_slot11prompt_saveER19server_prompt_cache(g_last_slot, g_current_cache);
    size_t cached_tokens = get_cache_n_tokens(g_current_cache);
    fprintf(
        stderr,
        "[nixloom-sleep] saved slot to prompt_cache (saved=%d, cached_tokens=%zu)\n",
        (int)saved,
        cached_tokens
    );
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

void _Z17common_fit_paramsPKcP18llama_model_paramsP20llama_context_paramsPfP32llama_model_tensor_buft_overridePmjPK22common_fit_extra_model14ggml_log_level(
    const char *path,
    void *mparams,
    void *cparams,
    float *tensor_split,
    void *tensor_buft_overrides,
    size_t *margins,
    unsigned int n_ctx_min,
    const void *extra_models,
    int log_level
) {
    static void (*real_fit_params)(
        const char *,
        void *,
        void *,
        float *,
        void *,
        size_t *,
        unsigned int,
        const void *,
        int
    ) = NULL;
    if (!real_fit_params) {
        real_fit_params = dlsym(
            RTLD_NEXT,
            "_Z17common_fit_paramsPKcP18llama_model_paramsP20llama_context_paramsPfP32llama_model_tensor_buft_overridePmjPK22common_fit_extra_model14ggml_log_level"
        );
    }
    struct llama_tensor_buft_override_ *overrides = (struct llama_tensor_buft_override_ *)tensor_buft_overrides;
    struct llama_model_params_prefix *mp = (struct llama_model_params_prefix *)mparams;
    struct llama_tensor_buft_override_ saved0 = { NULL, NULL };
    struct llama_tensor_buft_override_ saved1 = { NULL, NULL };
    if (overrides && overrides[0].pattern != NULL) {
        saved0 = overrides[0];
        saved1 = overrides[1];
        overrides[0].pattern = NULL;
        overrides[0].buft = NULL;
    }
    if (mp) {
        mp->tensor_buft_overrides = NULL;
    }
    real_fit_params(
        path,
        mparams,
        cparams,
        tensor_split,
        tensor_buft_overrides,
        margins,
        n_ctx_min,
        extra_models,
        log_level
    );
    if (mp && mp->tensor_buft_overrides == NULL && overrides && saved0.pattern != NULL) {
        overrides[0] = saved0;
        overrides[1] = saved1;
        mp->tensor_buft_overrides = overrides;
    }
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
        list_splice_back((struct cxx11_list_head *)self, &g_stashed_cache);
    }
    size_t before_tokens = get_cache_n_tokens(self);
    bool ok = real_load(self, prompt, tokens, ctx, ctx_dft, slot_id);
    size_t after_tokens = get_cache_n_tokens(self);
    size_t new_tokens = get_tokens_size(tokens);
    if (after_tokens == 0 || (after_tokens < before_tokens && new_tokens >= after_tokens)) {
        fprintf(
            stderr,
            "[nixloom-sleep] prompt_cache::load finished (ok=%d, new_tokens=%zu, cached_tokens=%zu->%zu), releasing RAM cache\n",
            (int)ok,
            new_tokens,
            before_tokens,
            after_tokens
        );
        clear_prompt_cache_list(self);
    } else {
        fprintf(
            stderr,
            "[nixloom-sleep] prompt_cache::load finished (ok=%d, new_tokens=%zu, cached_tokens=%zu->%zu), keeping RAM cache\n",
            (int)ok,
            new_tokens,
            before_tokens,
            after_tokens
        );
    }
    return ok;
}
