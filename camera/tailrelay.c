/*
 * tailrelay - TCP -> HTTP CONNECT relay for the Insta360 cloud uploader.
 *
 * Why this exists
 * ---------------
 * The camera firmware's curl never uses http_proxy and the SDK only sets
 * CURLOPT_PROXY when instaAIP's ClientConfig carries a proxy string (it is
 * empty in stock firmware).  We cannot make the uploader use a proxy.
 *
 * Instead we redirect the cloud hostname to 127.0.0.1 (dnsmasq) and listen on
 * 127.0.0.1:443 here.  Every accepted connection is tunneled through
 * tailscaled's userspace HTTP proxy:
 *
 *   tailscaled --tun=userspace-networking \
 *              --outbound-http-proxy-listen=127.0.0.1:8080
 *
 * TLS is end-to-end: this program only relays bytes, so the camera still
 * verifies the certificate against the hostname it requested.  The CONNECT
 * target is the home server's tailnet IP (or MagicDNS name).
 *
 * Build: see build_tailrelay.sh.  Freestanding, no libc, raw syscalls, so it
 * runs on the camera's AArch64 rootfs (and on x86_64 for tests).
 *
 * Usage:
 *   tailrelay LISTEN_PORT TARGET_HOST TARGET_PORT [PROXY_HOST] [PROXY_PORT]
 *             [LISTEN_ADDR]
 *
 * Defaults: PROXY_HOST=127.0.0.1 PROXY_PORT=8080 LISTEN_ADDR=127.0.0.1
 * TARGET_HOST is passed verbatim in the CONNECT line; use the tailnet IPv4
 * address if MagicDNS is not enabled.
 */

typedef unsigned long u64;
typedef long i64;
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;

/* ------------------------------------------------------------------ */
/* syscalls                                                            */
/* ------------------------------------------------------------------ */

#if defined(__aarch64__)
#define NR_READ 63
#define NR_WRITE 64
#define NR_CLOSE 57
#define NR_POLL 7
#define NR_SOCKET 198
#define NR_CONNECT 203
#define NR_ACCEPT 202
#define NR_SENDTO 206
#define NR_RECVFROM 207
#define NR_BIND 200
#define NR_LISTEN 201
#define NR_SETSOCKOPT 208
#define NR_SHUTDOWN 210
#define NR_CLONE 220
#define NR_WAIT4 260
#define NR_NANOSLEEP 101
#define NR_EXIT_GROUP 94
#elif defined(__x86_64__)
#define NR_READ 0
#define NR_WRITE 1
#define NR_CLOSE 3
#define NR_POLL 7
#define NR_SOCKET 41
#define NR_CONNECT 42
#define NR_ACCEPT 43
#define NR_SENDTO 44
#define NR_RECVFROM 45
#define NR_BIND 49
#define NR_LISTEN 50
#define NR_SETSOCKOPT 54
#define NR_SHUTDOWN 48
#define NR_FORK 57
#define NR_WAIT4 61
#define NR_NANOSLEEP 35
#define NR_EXIT_GROUP 231
#else
#error "unsupported architecture"
#endif

static inline i64 sc6(i64 n, i64 a, i64 b, i64 c, i64 d, i64 e, i64 f)
{
#if defined(__aarch64__)
    register i64 x8 __asm__("x8") = n;
    register i64 x0 __asm__("x0") = a;
    register i64 x1 __asm__("x1") = b;
    register i64 x2 __asm__("x2") = c;
    register i64 x3 __asm__("x3") = d;
    register i64 x4 __asm__("x4") = e;
    register i64 x5 __asm__("x5") = f;
    __asm__ volatile("svc #0"
                     : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
                     : "memory");
    return x0;
#else
    register i64 r10 __asm__("r10") = d;
    register i64 r8 __asm__("r8") = e;
    register i64 r9 __asm__("r9") = f;
    i64 ret;
    __asm__ volatile("syscall"
                     : "=a"(ret)
                     : "a"(n), "D"(a), "S"(b), "d"(c),
                       "r"(r10), "r"(r8), "r"(r9)
                     : "rcx", "r11", "memory");
    return ret;
#endif
}

#define sc0(n) sc6((n), 0, 0, 0, 0, 0, 0)
#define sc1(n, a) sc6((n), (i64)(a), 0, 0, 0, 0, 0)
#define sc2(n, a, b) sc6((n), (i64)(a), (i64)(b), 0, 0, 0, 0)
#define sc3(n, a, b, c) sc6((n), (i64)(a), (i64)(b), (i64)(c), 0, 0, 0)
#define sc4(n, a, b, c, d) sc6((n), (i64)(a), (i64)(b), (i64)(c), (i64)(d), 0, 0)
#define sc5(n, a, b, c, d, e) \
    sc6((n), (i64)(a), (i64)(b), (i64)(c), (i64)(d), (i64)(e), 0)
#define sc6a(n, a, b, c, d, e, f) \
    sc6((n), (i64)(a), (i64)(b), (i64)(c), (i64)(d), (i64)(e), (i64)(f))

static long sys_read(long fd, void *buf, long n) { return sc3(NR_READ, fd, buf, n); }
static long sys_write(long fd, const void *buf, long n) { return sc3(NR_WRITE, fd, buf, n); }
static long sys_close(long fd) { return sc1(NR_CLOSE, fd); }
static long sys_poll(void *p, long n, long t) { return sc3(NR_POLL, p, n, t); }
static long sys_socket(long d, long t, long p) { return sc3(NR_SOCKET, d, t, p); }
static long sys_connect(long fd, void *a, long l) { return sc3(NR_CONNECT, fd, a, l); }
static long sys_accept(long fd) { return sc3(NR_ACCEPT, fd, 0, 0); }
static long sys_bind(long fd, void *a, long l) { return sc3(NR_BIND, fd, a, l); }
static long sys_listen(long fd, long n) { return sc2(NR_LISTEN, fd, n); }
static long sys_setsockopt(long fd, long lvl, long opt, void *v, long l)
{
    return sc5(NR_SETSOCKOPT, fd, lvl, opt, v, l);
}
static long sys_shutdown(long fd, long how) { return sc2(NR_SHUTDOWN, fd, how); }
static long sys_sendto(long fd, const void *buf, long n, long flags)
{
    return sc6a(NR_SENDTO, fd, buf, n, flags, 0, 0);
}
static long sys_recvfrom(long fd, void *buf, long n)
{
    return sc6a(NR_RECVFROM, fd, buf, n, 0, 0, 0);
}
static long sys_wait4_nohang(void) { return sc6a(NR_WAIT4, -1, 0, 1, 0, 0, 0); }
static void sys_exit_group(long code) { sc1(NR_EXIT_GROUP, code); for (;;) {} }

static long sys_fork(void)
{
#if defined(__aarch64__)
    /* arm64 has no fork(2); clone(SIGCHLD, NULL, ...) is what libc uses. */
    return sc6a(NR_CLONE, 17, 0, 0, 0, 0, 0);
#else
    return sc0(NR_FORK);
#endif
}

/* ------------------------------------------------------------------ */
/* constants                                                           */
/* ------------------------------------------------------------------ */

#define AF_INET 2
#define SOCK_STREAM 1
#define SOL_SOCKET 1
#define SO_REUSEADDR 2
#define POLLIN 1
#define POLLERR 8
#define POLLHUP 16
#define MSG_NOSIGNAL 0x4000
#define SHUT_WR 1

#define BUF_SIZE 16384
#define HDR_MAX 1024

struct sockaddr_in {
    u16 family;
    u16 port;
    u32 addr;
    u8 pad[8];
};

struct pollfd {
    int fd;
    short events;
    short revents;
};

/* ------------------------------------------------------------------ */
/* tiny string helpers                                                 */
/* ------------------------------------------------------------------ */

static u64 s_len(const char *s)
{
    u64 n = 0;
    while (s[n]) n++;
    return n;
}

static void s_copy(char *d, const char *s, u64 max)
{
    u64 i = 0;
    if (max == 0) return;
    while (s[i] && i + 1 < max) {
        d[i] = s[i];
        i++;
    }
    d[i] = 0;
}

static int s_eq(const char *a, const char *b)
{
    while (*a && *b) {
        if (*a != *b) return 0;
        a++;
        b++;
    }
    return *a == *b;
}

static int s_find(const char *hay, u64 hlen, const char *needle)
{
    u64 nlen = s_len(needle);
    if (nlen == 0 || nlen > hlen) return 0;
    for (u64 i = 0; i + nlen <= hlen; i++) {
        u64 j = 0;
        while (j < nlen && hay[i + j] == needle[j]) j++;
        if (j == nlen) return 1;
    }
    return 0;
}

static int parse_u16(const char *s, int *out)
{
    long v = 0;
    if (!s || !*s) return -1;
    while (*s) {
        if (*s < '0' || *s > '9') return -1;
        v = v * 10 + (*s - '0');
        if (v > 65535) return -1;
        s++;
    }
    *out = (int)v;
    return 0;
}

static int parse_ipv4(const char *s, u32 *out)
{
    u32 addr = 0;
    int part = 0;
    int val = 0;
    int digits = 0;
    if (!s) return -1;
    while (1) {
        char c = *s;
        if (c >= '0' && c <= '9') {
            val = val * 10 + (c - '0');
            if (val > 255) return -1;
            digits = 1;
            s++;
            continue;
        }
        if (!digits) return -1;
        /* in_addr is kept in network byte order in memory; on a little-endian
         * host that means octet 1 is the low byte of the u32. */
        addr |= ((u32)val) << (8 * part);
        part++;
        val = 0;
        digits = 0;
        if (c == '.') {
            if (part >= 4) return -1;
            s++;
            continue;
        }
        if (c == 0) break;
        return -1;
    }
    if (part != 4) return -1;
    *out = addr;
    return 0;
}

static u16 htons(u16 v)
{
    return (u16)((v << 8) | (v >> 8));
}

static void buf_add(char *dst, u64 *pos, u64 max, const char *src)
{
    while (*src && *pos + 1 < max) dst[(*pos)++] = *src++;
    dst[*pos] = 0;
}

static void buf_add_int(char *dst, u64 *pos, u64 max, long v)
{
    char tmp[8];
    int n = 0;
    if (v == 0) {
        if (*pos + 1 < max) dst[(*pos)++] = '0';
        dst[*pos] = 0;
        return;
    }
    while (v > 0 && n < 8) {
        tmp[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (n > 0 && *pos + 1 < max) dst[(*pos)++] = tmp[--n];
    dst[*pos] = 0;
}

static void write_str(const char *s)
{
    sys_write(2, s, (long)s_len(s));
}

static void write_int(long v)
{
    char b[24];
    int n = 0;
    if (v == 0) {
        sys_write(2, "0", 1);
        return;
    }
    if (v < 0) {
        sys_write(2, "-", 1);
        v = -v;
    }
    while (v > 0 && n < 23) {
        b[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (n > 0) sys_write(2, &b[--n], 1);
}

/* ------------------------------------------------------------------ */
/* relay                                                               */
/* ------------------------------------------------------------------ */

static void splice2(long a, long b)
{
    char buf[BUF_SIZE];
    struct pollfd p[2];
    p[0].fd = (int)a;
    p[0].events = POLLIN;
    p[1].fd = (int)b;
    p[1].events = POLLIN;

    while (p[0].fd >= 0 || p[1].fd >= 0) {
        p[0].revents = 0;
        p[1].revents = 0;
        long r = sys_poll(p, 2, -1);
        if (r < 0) {
            if (r == -4) continue; /* EINTR */
            break;
        }
        for (int i = 0; i < 2; i++) {
            long fd = p[i].fd;
            if (fd < 0) continue;
            if (!(p[i].revents & (POLLIN | POLLHUP | POLLERR))) continue;

            long n = sys_recvfrom(fd, buf, BUF_SIZE);
            if (n > 0) {
                long other = p[1 - i].fd;
                if (other < 0) continue;
                if (sys_sendto(other, buf, n, MSG_NOSIGNAL) != n) {
                    sys_shutdown(p[0].fd >= 0 ? p[0].fd : p[1].fd, SHUT_WR);
                    p[0].fd = -1;
                    p[1].fd = -1;
                    break;
                }
            } else {
                /* EOF (or error): half-close the other direction and stop
                 * polling this side. */
                p[i].fd = -1;
                long other = p[1 - i].fd;
                if (other >= 0) sys_shutdown(other, SHUT_WR);
            }
        }
    }

    if (a >= 0) sys_close(a);
    if (b >= 0) sys_close(b);
}

/*
 * Open a CONNECT tunnel through the local proxy.  Returns the connected
 * socket, or -1.  Exchanges no application bytes.
 */
static long proxy_connect(const char *target, int tport,
                          u32 proxy_addr, int proxy_port)
{
    char req[512];
    u64 pos = 0;
    u8 hdr[HDR_MAX];
    long hlen = 0;

    long pfd = sys_socket(AF_INET, SOCK_STREAM, 0);
    if (pfd < 0) return -1;

    struct sockaddr_in sa;
    for (u64 i = 0; i < sizeof(sa); i++) ((u8 *)&sa)[i] = 0;
    sa.family = AF_INET;
    sa.port = htons((u16)proxy_port);
    sa.addr = proxy_addr;

    if (sys_connect(pfd, &sa, sizeof(sa)) < 0) {
        sys_close(pfd);
        return -1;
    }

    req[0] = 0;
    buf_add(req, &pos, sizeof(req), "CONNECT ");
    buf_add(req, &pos, sizeof(req), target);
    buf_add(req, &pos, sizeof(req), ":");
    buf_add_int(req, &pos, sizeof(req), tport);
    buf_add(req, &pos, sizeof(req), " HTTP/1.1\r\nHost: ");
    buf_add(req, &pos, sizeof(req), target);
    buf_add(req, &pos, sizeof(req), ":");
    buf_add_int(req, &pos, sizeof(req), tport);
    buf_add(req, &pos, sizeof(req), "\r\n\r\n");

    long off = 0;
    long total = (long)pos;
    while (off < total) {
        long n = sys_write(pfd, req + off, total - off);
        if (n <= 0) {
            sys_close(pfd);
            return -1;
        }
        off += n;
    }

    /* Read the proxy response one byte at a time so no byte beyond the
     * header terminator is consumed (it would belong to the TLS stream). */
    while (hlen < HDR_MAX - 1) {
        long n = sys_read(pfd, hdr + hlen, 1);
        if (n != 1) break;
        hlen++;
        if (hlen >= 4 &&
            hdr[hlen - 4] == '\r' && hdr[hlen - 3] == '\n' &&
            hdr[hlen - 2] == '\r' && hdr[hlen - 1] == '\n')
            break;
    }
    hdr[hlen] = 0;

    if (!s_find((const char *)hdr, (u64)hlen, " 200 ")) {
        sys_close(pfd);
        return -1;
    }

    return pfd;
}

/*
 * Handle one camera connection.  The camera speaks TLS immediately, so we
 * cannot see an HTTP request: we always CONNECT to the configured target.
 */
static void handle_one(long cfd, const char *target, int tport,
                       u32 proxy_addr, int proxy_port)
{
    long pfd = proxy_connect(target, tport, proxy_addr, proxy_port);
    if (pfd < 0) return;
    splice2(cfd, pfd);
}

static int main(int argc, char **argv)
{
    int listen_port = 0, target_port = 0, proxy_port = 8080;
    u32 proxy_addr = 0x0100007f; /* 127.0.0.1 in network byte order */
    u32 listen_addr = 0x0100007f;
    const char *target = 0;
    const char *proxy_host = "127.0.0.1";
    const char *listen_host = "127.0.0.1";

    if (argc >= 4 && s_eq(argv[1], "--probe")) {
        if (parse_u16(argv[3], &target_port) < 0) {
            write_str("tailrelay: bad target port\n");
            return 2;
        }
        target = argv[2];
        if (!*target) {
            write_str("tailrelay: empty target\n");
            return 2;
        }
        for (const char *p = target; *p; p++) {
            if (*p == '\r' || *p == '\n' || *p == ' ' || *p == '\t') {
                write_str("tailrelay: invalid target\n");
                return 2;
            }
        }
        if (argc >= 5) proxy_host = argv[4];
        if (argc >= 6 && parse_u16(argv[5], &proxy_port) < 0) {
            write_str("tailrelay: bad proxy port\n");
            return 2;
        }
        if (parse_ipv4(proxy_host, &proxy_addr) < 0) {
            write_str("tailrelay: bad proxy address\n");
            return 2;
        }
        long pfd = proxy_connect(target, target_port, proxy_addr, proxy_port);
        if (pfd < 0) {
            write_str("probe failed\n");
            return 1;
        }
        sys_close(pfd);
        write_str("probe ok\n");
        return 0;
    }

    if (argc < 4) {
        write_str("usage: tailrelay LISTEN_PORT TARGET_HOST TARGET_PORT "
                  "[PROXY_HOST] [PROXY_PORT] [LISTEN_ADDR]\n");
        return 2;
    }
    if (parse_u16(argv[1], &listen_port) < 0 ||
        parse_u16(argv[3], &target_port) < 0) {
        write_str("tailrelay: bad port\n");
        return 2;
    }
    target = argv[2];
    if (!*target) {
        write_str("tailrelay: empty target\n");
        return 2;
    }
    /* Reject characters that would break the CONNECT request line. */
    for (const char *p = target; *p; p++) {
        if (*p == '\r' || *p == '\n' || *p == ' ' || *p == '\t') {
            write_str("tailrelay: invalid target\n");
            return 2;
        }
    }

    if (argc >= 5) proxy_host = argv[4];
    if (argc >= 6 && parse_u16(argv[5], &proxy_port) < 0) {
        write_str("tailrelay: bad proxy port\n");
        return 2;
    }
    if (argc >= 7) listen_host = argv[6];

    if (parse_ipv4(proxy_host, &proxy_addr) < 0) {
        write_str("tailrelay: proxy host must be an IPv4 address\n");
        return 2;
    }
    if (parse_ipv4(listen_host, &listen_addr) < 0) {
        write_str("tailrelay: listen host must be an IPv4 address\n");
        return 2;
    }

    long lfd = sys_socket(AF_INET, SOCK_STREAM, 0);
    if (lfd < 0) {
        write_str("tailrelay: socket failed errno=");
        write_int(-lfd);
        write_str("\n");
        return 1;
    }

    int one = 1;
    sys_setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in la;
    for (u64 i = 0; i < sizeof(la); i++) ((u8 *)&la)[i] = 0;
    la.family = AF_INET;
    la.port = htons((u16)listen_port);
    la.addr = listen_addr;

    long rc = sys_bind(lfd, &la, sizeof(la));
    if (rc < 0) {
        write_str("tailrelay: bind failed errno=");
        write_int(-rc);
        write_str("\n");
        return 1;
    }
    rc = sys_listen(lfd, 16);
    if (rc < 0) {
        write_str("tailrelay: listen failed errno=");
        write_int(-rc);
        write_str("\n");
        return 1;
    }

    for (;;) {
        long cfd = sys_accept(lfd);
        if (cfd < 0) {
            if (cfd == -4) continue; /* EINTR */
            /* EMFILE/ENFILE: back off instead of spinning. */
            struct { long sec; long nsec; } ts = {1, 0};
            sc2(NR_NANOSLEEP, &ts, 0);
            continue;
        }

        long pid = sys_fork();
        if (pid == 0) {
            sys_close(lfd);
            handle_one(cfd, target, target_port, proxy_addr, proxy_port);
            sys_exit_group(0);
        }
        sys_close(cfd);
        while (sys_wait4_nohang() > 0) { }
    }
}

/* ------------------------------------------------------------------ */
/* entry point                                                         */
/* ------------------------------------------------------------------ */

#if defined(__aarch64__)
__asm__(
    ".global _start\n"
    "_start:\n"
    "mov x0, sp\n"
    "bl start_c\n"
    "mov x0, #0\n"
    "mov x8, #94\n"
    "svc #0\n");
#else
__asm__(
    ".global _start\n"
    "_start:\n"
    "mov %rsp, %rdi\n"
    "and $-16, %rsp\n"
    "call start_c\n"
    "xor %edi, %edi\n"
    "mov $231, %eax\n"
    "syscall\n");
#endif

void start_c(long *sp)
{
    long argc = sp[0];
    char **argv = (char **)&sp[1];
    sys_exit_group(main((int)argc, argv));
}
