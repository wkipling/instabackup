/*
 * insupload - push Insta360 X4 Air media to a self-hosted receiver.
 *
 * Runs on the camera (aarch64, musl static) after bootup.sh has brought up
 * Wi-Fi and Tailscale.  It walks the media tree, asks the receiver how much
 * of each file it already has, and appends the missing bytes.  A dropped
 * hotel hotspot therefore resumes instead of restarting a 5 GB clip.
 *
 * Wire protocol (plain HTTP; the transport is already WireGuard via
 * Tailscale, so TLS would only add CPU cost and a CA dependency):
 *
 *   GET  /api/stat?path=REL            -> {"size":N,"final":true|false}
 *   PUT  /api/upload?path=REL          X-Offset:N X-Total-Size:M  (body)
 *   POST /api/reset?path=REL[&all=1]
 *
 * Exit codes: 0 = pass completed, 2 = receiver unreachable, 3 = some files
 * failed but the pass completed.
 */

#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netdb.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define CHUNK (1 << 20)
#define RESP_CAP (64 << 10)
#define CONNECT_TIMEOUT_MS 15000
#define IO_TIMEOUT_S 45

static char g_host[256];
static int g_port = 8844;

/* Two endpoints: the tailnet receiver (through tailscaled's proxy) and an
 * optional direct LAN receiver.  The uploader uses whichever answers first,
 * so a broken tunnel still backs up at home. */
struct endpoint {
    char host[256];
    int port;
    char proxy_host[128];
    int proxy_port;
};
static struct endpoint g_ep[2];
static int g_ep_count = 1;
static int g_active = 0;
#define EP (g_ep[g_active])

static char g_token[256];
static const char *g_root = "/tmp/SD0/DCIM";
static int g_delete;
static long g_min_age = 30;
static int g_once;
static int g_interval = 300;
static int g_wait = 600;
static int g_verbose;

struct stats {
    int uploaded;
    int skipped;
    int failed;
    long long bytes;
};

/* Set when the receiver becomes unreachable mid-pass.  Continuing would
 * spend the retry budget on every remaining file, so the pass stops and the
 * next one (60 s later) starts when the link is back. */
static int g_abort_pass;

static void logmsg(const char *fmt, ...)
{
    char ts[32];
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm);
    fprintf(stderr, "%s ", ts);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

/* ------------------------------------------------------------------ */
/* HTTP client                                                         */
/* ------------------------------------------------------------------ */

static int send_all(int fd, const void *buf, size_t len);

static int tcp_connect(void)
{
    char portstr[16];
    struct addrinfo hints, *res = NULL, *ai;
    int fd = -1;
    const char *dial_host = EP.proxy_port ? EP.proxy_host : EP.host;
    int dial_port = EP.proxy_port ? EP.proxy_port : EP.port;

    snprintf(portstr, sizeof(portstr), "%d", dial_port);
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    int rc = getaddrinfo(dial_host, portstr, &hints, &res);
    if (rc != 0) {
        logmsg("resolve %s failed: %s", dial_host, gai_strerror(rc));
        return -1;
    }

    for (ai = res; ai != NULL; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype | SOCK_CLOEXEC, ai->ai_protocol);
        if (fd < 0)
            continue;

        int flags = fcntl(fd, F_GETFL, 0);
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
        rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
        if (rc < 0 && errno == EINPROGRESS) {
            struct pollfd pfd = { .fd = fd, .events = POLLOUT };
            rc = poll(&pfd, 1, CONNECT_TIMEOUT_MS);
            if (rc > 0) {
                int err = 0;
                socklen_t elen = sizeof(err);
                getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &elen);
                rc = err ? -1 : 0;
            } else {
                rc = -1;
            }
        }
        if (rc == 0) {
            fcntl(fd, F_SETFL, flags);
            struct timeval tv = { .tv_sec = IO_TIMEOUT_S };
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
            break;
        }
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);

    if (fd >= 0 && EP.proxy_port) {
        /* tailscaled userspace-networking mode has no tun device: outbound
         * tailnet traffic must go through its HTTP CONNECT proxy. */
        char creq[600];
        int n = snprintf(creq, sizeof(creq),
                         "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n",
                         EP.host, EP.port, EP.host, EP.port);
        if (n < 0 || send_all(fd, creq, (size_t)n) < 0) {
            close(fd);
            return -1;
        }
        char head[512];
        size_t used = 0;
        while (used + 1 < sizeof(head)) {
            if (recv(fd, head + used, 1, 0) != 1)
                break;
            used++;
            if (used >= 4 && memcmp(head + used - 4, "\r\n\r\n", 4) == 0)
                break;
        }
        head[used] = '\0';
        if (used < 12 || strncmp(head, "HTTP/1.", 7) != 0 ||
            head[9] != '2') {
            logmsg("proxy CONNECT %s:%d failed: %.80s", EP.host, EP.port, head);
            close(fd);
            return -1;
        }
    }
    return fd;
}

static int send_all(int fd, const void *buf, size_t len)
{
    const char *p = buf;
    while (len > 0) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (n == 0)
            return -1;
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int read_response(int fd, char *buf, size_t cap)
{
    size_t used = 0;
    while (used + 1 < cap) {
        ssize_t n = recv(fd, buf + used, cap - 1 - used, 0);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (n == 0)
            break;
        used += (size_t)n;
    }
    buf[used] = '\0';
    return (int)used;
}

static int status_code(const char *resp)
{
    int code = 0;
    if (resp == NULL)
        return 0;
    if (sscanf(resp, "HTTP/%*d.%*d %d", &code) != 1)
        return 0;
    return code;
}

/* Send a request; when body_fd >= 0, stream body_len bytes at body_off. */
static int http_do(const char *method, const char *path, const char *extra,
                   int body_fd, long long body_off, long long body_len,
                   long long content_len, char *resp, size_t cap)
{
    int fd = tcp_connect();
    if (fd < 0)
        return -1;

    char head[1024];
    int n = snprintf(head, sizeof(head),
                     "%s %s HTTP/1.1\r\n"
                     "Host: %s:%d\r\n"
                     "User-Agent: insupload/1\r\n"
                     "Accept: application/json\r\n"
                     "Connection: close\r\n"
                     "%s%s%s"
                     "%s"
                     "Content-Length: %lld\r\n\r\n",
                     method, path, EP.host, EP.port,
                     g_token[0] ? "X-Auth-Token: " : "",
                     g_token, g_token[0] ? "\r\n" : "",
                     extra ? extra : "",
                     content_len);
    if (n < 0 || (size_t)n >= sizeof(head) || send_all(fd, head, (size_t)n) < 0) {
        close(fd);
        return -1;
    }

    if (body_fd >= 0) {
        char *buf = malloc(CHUNK);
        if (buf == NULL) {
            close(fd);
            return -1;
        }
        long long left = body_len;
        long long off = body_off;
        while (left > 0) {
            size_t want = left > CHUNK ? CHUNK : (size_t)left;
            ssize_t got = pread(body_fd, buf, want, (off_t)off);
            if (got <= 0) {
                free(buf);
                close(fd);
                return -1;
            }
            if (send_all(fd, buf, (size_t)got) < 0) {
                free(buf);
                close(fd);
                return -1;
            }
            off += got;
            left -= got;
        }
        free(buf);
    }

    int rc = read_response(fd, resp, cap);
    close(fd);
    return rc < 0 ? -1 : 0;
}

static int json_int(const char *resp, const char *key, long long *out)
{
    const char *p = resp ? strstr(resp, key) : NULL;
    if (p == NULL)
        return -1;
    p += strlen(key);
    while (*p == ' ' || *p == ':')
        p++;
    char *end = NULL;
    long long v = strtoll(p, &end, 10);
    if (end == p)
        return -1;
    *out = v;
    return 0;
}

static int json_bool(const char *resp, const char *key, int *out)
{
    const char *p = resp ? strstr(resp, key) : NULL;
    if (p == NULL)
        return -1;
    p += strlen(key);
    while (*p == ' ' || *p == ':')
        p++;
    if (strncmp(p, "true", 4) == 0) {
        *out = 1;
        return 0;
    }
    if (strncmp(p, "false", 5) == 0) {
        *out = 0;
        return 0;
    }
    return -1;
}

/* Percent-encode a relative path, leaving '/' and RFC3986 unreserved. */
static void enc_path(char *dst, size_t cap, const char *src)
{
    static const char hex[] = "0123456789ABCDEF";
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p && o + 4 < cap; p++) {
        if (isalnum(*p) || *p == '-' || *p == '_' || *p == '.' || *p == '~' || *p == '/') {
            dst[o++] = (char)*p;
        } else {
            dst[o++] = '%';
            dst[o++] = hex[*p >> 4];
            dst[o++] = hex[*p & 15];
        }
    }
    dst[o] = '\0';
}

/* ------------------------------------------------------------------ */
/* Receiver API                                                        */
/* ------------------------------------------------------------------ */

/* 0 = ok, -1 = transport error, otherwise HTTP status. */
static int api_stat(const char *rel, long long *size, int *final)
{
    char enc[4096], path[4200], resp[RESP_CAP];
    enc_path(enc, sizeof(enc), rel);
    snprintf(path, sizeof(path), "/api/stat?path=%s", enc);
    if (http_do("GET", path, "", -1, 0, 0, 0, resp, sizeof(resp)) < 0)
        return -1;
    int code = status_code(resp);
    if (code == 200) {
        if (json_int(resp, "\"size\"", size) < 0)
            *size = 0;
        if (json_bool(resp, "\"final\"", final) < 0)
            *final = 0;
    }
    return code;
}

static int api_reset(const char *rel, int all)
{
    char enc[4096], path[4200], resp[RESP_CAP];
    enc_path(enc, sizeof(enc), rel);
    snprintf(path, sizeof(path), "/api/reset?path=%s%s", enc, all ? "&all=1" : "");
    if (http_do("POST", path, "", -1, 0, 0, 0, resp, sizeof(resp)) < 0)
        return -1;
    int code = status_code(resp);
    logmsg("reset %s (all=%d) -> %d", rel, all, code);
    return code;
}

/* Upload body [off, off+len) of the file at full. Returns new server size. */
static int api_put(const char *rel, int fd, long long off, long long len,
                   long long total, long long *newsize, int *final)
{
    char enc[4096], path[4200], resp[RESP_CAP], extra[128];
    enc_path(enc, sizeof(enc), rel);
    snprintf(path, sizeof(path), "/api/upload?path=%s", enc);
    snprintf(extra, sizeof(extra), "X-Offset: %lld\r\nX-Total-Size: %lld\r\n", off, total);
    if (http_do("PUT", path, extra, fd, off, len, len, resp, sizeof(resp)) < 0)
        return -1;
    int code = status_code(resp);
    *newsize = off;
    *final = 0;
    json_int(resp, "\"size\"", newsize);
    json_bool(resp, "\"final\"", final);
    if (code != 200 && g_verbose)
        logmsg("put %s @%lld+%lld -> %d: %.160s", rel, off, len, code, resp);
    return code;
}

static int api_ping(void)
{
    char resp[RESP_CAP];
    if (http_do("GET", "/api/ping", "", -1, 0, 0, 0, resp, sizeof(resp)) < 0)
        return -1;
    return status_code(resp);
}

/* ------------------------------------------------------------------ */
/* Files                                                               */
/* ------------------------------------------------------------------ */

static int has_media_ext(const char *name)
{
    static const char *exts[] = {
        ".insv", ".insp", ".lrv", ".jpg", ".jpeg", ".dng",
        ".mp4", ".mov", ".png", ".heic", ".exr", NULL
    };
    const char *dot = strrchr(name, '.');
    if (dot == NULL)
        return 0;
    for (int i = 0; exts[i]; i++) {
        const char *a = dot, *b = exts[i];
        while (*a && *b && tolower((unsigned char)*a) == *b) {
            a++;
            b++;
        }
        if (*a == '\0' && *b == '\0')
            return 1;
    }
    return 0;
}

/*
 * Ask the receiver for its state, then append whatever is missing.
 * Returns 0 when the receiver holds the complete file.
 */
static int upload_one(const char *full, const char *rel, struct stats *st)
{
    struct stat sb;
    if (stat(full, &sb) != 0 || !S_ISREG(sb.st_mode) || sb.st_size <= 0)
        return 0;

    time_t now = time(NULL);
    if (g_min_age > 0 && now - sb.st_mtime < g_min_age) {
        if (g_verbose)
            logmsg("skip (recent) %s", rel);
        return 0;
    }

    long long total = (long long)sb.st_size;
    long long off = 0;
    int final = 0;

    for (int attempt = 0; attempt < 5; attempt++) {
        int code = api_stat(rel, &off, &final);
        if (code < 0) {
            logmsg("stat %s failed: receiver unreachable", rel);
            g_abort_pass = 1;
            return -1;
        }
        if (code == 200 && final && off == total) {
            st->skipped++;
            if (g_delete && unlink(full) == 0)
                logmsg("already on server, deleted local %s", rel);
            return 0;
        }
        if (code == 200 && (final || off > total)) {
            /* Server has a different/complete copy: start over. */
            if (api_reset(rel, 1) < 0)
                return -1;
            off = 0;
            final = 0;
        }
        if (code != 200 && code != 404) {
            logmsg("stat %s -> HTTP %d", rel, code);
            return -1;
        }

        int fd = open(full, O_RDONLY | O_CLOEXEC);
        if (fd < 0) {
            logmsg("open %s failed: %s", rel, strerror(errno));
            return -1;
        }

        logmsg("upload %s from %lld/%lld", rel, off, total);
        int failed = 0;
        long long last_log = off;
        while (off < total) {
            long long want = total - off;
            if (want > CHUNK)
                want = CHUNK;
            long long newsize = off;
            int newfinal = 0;
            int rc = api_put(rel, fd, off, want, total, &newsize, &newfinal);
            if (rc < 0) {
                logmsg("connection lost at %lld/%lld", off, total);
                failed = 1;
                break;
            }
            if (rc != 200) {
                logmsg("put %s -> HTTP %d at %lld", rel, rc, off);
                failed = 1;
                break;
            }
            if (newsize <= off) {
                logmsg("server made no progress at %lld", off);
                failed = 1;
                break;
            }
            off = newsize;
            final = newfinal;
            if (off - last_log >= (64 << 20) || off == total) {
                logmsg("  %s %lld/%lld (%lld%%)", rel, off, total,
                       total ? (off * 100 / total) : 100);
                last_log = off;
            }
        }
        close(fd);

        if (!failed && off == total) {
            if (!final) {
                /* Zero-length PUT finalises a file whose last chunk landed
                 * but whose rename never happened. */
                int fd2 = open(full, O_RDONLY | O_CLOEXEC);
                long long newsize = off;
                int newfinal = 0;
                int rc = fd2 >= 0 ? api_put(rel, fd2, off, 0, total, &newsize, &newfinal) : -1;
                if (fd2 >= 0)
                    close(fd2);
                if (rc == 200 && newfinal) {
                    final = 1;
                }
            }
            if (final) {
                st->uploaded++;
                st->bytes += total;
                logmsg("done %s (%lld bytes)", rel, total);
                if (g_delete && unlink(full) == 0)
                    logmsg("deleted local %s", rel);
                return 0;
            }
        }
        sleep(5 * (attempt + 1));
    }

    st->failed++;
    logmsg("FAILED %s", rel);
    g_abort_pass = 1;
    return -1;
}

static void walk(const char *dir, const char *prefix, struct stats *st)
{
    DIR *dp = opendir(dir);
    if (dp == NULL)
        return;

    struct dirent *de;
    while ((de = readdir(dp)) != NULL) {
        if (g_abort_pass)
            break;
        if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0)
            continue;
        if (de->d_name[0] == '.')
            continue;

        char full[PATH_MAX];
        char rel[PATH_MAX];
        if (snprintf(full, sizeof(full), "%s/%s", dir, de->d_name) >= (int)sizeof(full))
            continue;
        if (prefix[0])
            snprintf(rel, sizeof(rel), "%s/%s", prefix, de->d_name);
        else
            snprintf(rel, sizeof(rel), "%s", de->d_name);

        struct stat sb;
        if (lstat(full, &sb) != 0)
            continue;
        if (S_ISDIR(sb.st_mode)) {
            if (strcmp(de->d_name, "tailscale") == 0)
                continue;
            walk(full, rel, st);
            if (g_abort_pass)
                break;
        } else if (S_ISREG(sb.st_mode) && has_media_ext(de->d_name)) {
            upload_one(full, rel, st);
        }
    }
    closedir(dp);
}

static int wait_for_receiver(void)
{
    int waited = 0;
    while (1) {
        for (int i = 0; i < g_ep_count; i++) {
            g_active = i;
            int code = api_ping();
            if (code == 200) {
                if (i > 0)
                    logmsg("primary unreachable; using fallback %s:%d",
                           EP.host, EP.port);
                else if (waited)
                    logmsg("receiver reachable after %ds", waited);
                return 0;
            }
        }
        if (waited >= g_wait) {
            logmsg("receiver still unreachable after %ds; giving up this pass", waited);
            return -1;
        }
        if (waited == 0) {
            logmsg("waiting for receiver at %s:%d%s ...",
                   g_ep[0].host, g_ep[0].port,
                   g_ep_count > 1 ? " (and LAN fallback)" : "");
        }
        sleep(10);
        waited += 10;
    }
}

static void usage(void)
{
    fprintf(stderr,
            "usage: insupload --url http://HOST:PORT [options]\n"
            "  --url2 URL        direct endpoint to try when --url is down (LAN)\n"
            "  --root DIR        media tree (default /tmp/SD0/DCIM)\n"
            "  --token T         shared token (or --token-file F)\n"
            "  --token-file F    read token from file\n"
            "  --proxy H:P       HTTP CONNECT proxy (tailscaled userspace mode)\n"
            "  --min-age SEC     skip files modified within SEC (default 30)\n"
            "  --interval SEC    delay between passes in loop mode (default 300)\n"
            "  --wait SEC        how long to wait for the receiver (default 600)\n"
            "  --delete          remove a file after the server confirms it\n"
            "  --once            one pass and exit\n"
            "  --verbose         log skipped files too\n");
}

int main(int argc, char **argv)
{
    const char *url = NULL;
    const char *url2 = NULL;
    const char *token_file = NULL;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "--url") == 0 && i + 1 < argc) {
            url = argv[++i];
        } else if (strcmp(a, "--url2") == 0 && i + 1 < argc) {
            url2 = argv[++i];
        } else if (strcmp(a, "--token") == 0 && i + 1 < argc) {
            snprintf(g_token, sizeof(g_token), "%s", argv[++i]);
        } else if (strcmp(a, "--token-file") == 0 && i + 1 < argc) {
            token_file = argv[++i];
        } else if (strcmp(a, "--proxy") == 0 && i + 1 < argc) {
            snprintf(g_ep[0].proxy_host, sizeof(g_ep[0].proxy_host), "%s", argv[++i]);
            char *c = strrchr(g_ep[0].proxy_host, ':');
            if (c != NULL) {
                *c = '\0';
                g_ep[0].proxy_port = atoi(c + 1);
            }
            if (g_ep[0].proxy_port <= 0 || g_ep[0].proxy_port > 65535) {
                logmsg("bad --proxy (want HOST:PORT)");
                return 1;
            }
        } else if (strcmp(a, "--root") == 0 && i + 1 < argc) {
            g_root = argv[++i];
        } else if (strcmp(a, "--min-age") == 0 && i + 1 < argc) {
            g_min_age = atol(argv[++i]);
        } else if (strcmp(a, "--interval") == 0 && i + 1 < argc) {
            g_interval = atoi(argv[++i]);
        } else if (strcmp(a, "--wait") == 0 && i + 1 < argc) {
            g_wait = atoi(argv[++i]);
        } else if (strcmp(a, "--delete") == 0) {
            g_delete = 1;
        } else if (strcmp(a, "--once") == 0) {
            g_once = 1;
        } else if (strcmp(a, "--verbose") == 0) {
            g_verbose = 1;
        } else {
            usage();
            return 1;
        }
    }
    if (url == NULL) {
        usage();
        return 1;
    }

    if (token_file != NULL) {
        FILE *fh = fopen(token_file, "r");
        if (fh != NULL) {
            if (fgets(g_token, sizeof(g_token), fh) != NULL) {
                char *p = g_token + strlen(g_token);
                while (p > g_token && (p[-1] == '\n' || p[-1] == '\r' || p[-1] == ' '))
                    *--p = '\0';
            }
            fclose(fh);
        }
    }

    const char *p = strstr(url, "://");
    p = p ? p + 3 : url;
    const char *slash = strchr(p, '/');
    size_t hostlen = slash ? (size_t)(slash - p) : strlen(p);
    char hostport[300];
    if (hostlen >= sizeof(hostport))
        return 1;
    memcpy(hostport, p, hostlen);
    hostport[hostlen] = '\0';

    char *colon = strrchr(hostport, ':');
    if (colon != NULL) {
        *colon = '\0';
        g_port = atoi(colon + 1);
    }
    snprintf(g_host, sizeof(g_host), "%s", hostport);
    if (g_port <= 0 || g_port > 65535)
        g_port = 8844;
    snprintf(g_ep[0].host, sizeof(g_ep[0].host), "%s", g_host);
    g_ep[0].port = g_port;

    if (url2 != NULL) {
        const char *q = strstr(url2, "://");
        q = q ? q + 3 : url2;
        const char *s2 = strchr(q, '/');
        size_t len2 = s2 ? (size_t)(s2 - q) : strlen(q);
        char hp2[300];
        if (len2 < sizeof(hp2)) {
            memcpy(hp2, q, len2);
            hp2[len2] = '\0';
            int port2 = 8844;
            char *c2 = strrchr(hp2, ':');
            if (c2 != NULL) {
                *c2 = '\0';
                port2 = atoi(c2 + 1);
            }
            if (port2 > 0 && port2 <= 65535) {
                snprintf(g_ep[1].host, sizeof(g_ep[1].host), "%s", hp2);
                g_ep[1].port = port2;
                g_ep_count = 2;
            }
        }
    }

    logmsg("insupload start: root=%s primary=%s:%d%s fallback=%s proxy=%s token=%s delete=%d",
           g_root, g_ep[0].host, g_ep[0].port,
           g_ep_count > 1 ? "+LAN" : "",
           g_ep_count > 1 ? g_ep[1].host : "none",
           g_ep[0].proxy_port ? g_ep[0].proxy_host : "none",
           g_token[0] ? "yes" : "no", g_delete);

    signal(SIGPIPE, SIG_IGN);

    while (1) {
        struct stats st = {0, 0, 0, 0};
        g_abort_pass = 0;
        if (wait_for_receiver() == 0) {
            walk(g_root, "", &st);
            logmsg("pass complete: uploaded=%d skipped=%d failed=%d bytes=%lld%s",
                   st.uploaded, st.skipped, st.failed, st.bytes,
                   g_abort_pass ? " (aborted: receiver lost)" : "");
            if (g_once)
                return st.failed ? 3 : 0;
        } else if (g_once) {
            return 2;
        }
        sleep(g_interval);
    }
}
