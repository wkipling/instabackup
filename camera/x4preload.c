// X4 bootstrap: a dependency-free ELF preload library.
//
// The X4 1.9.21 init script does not run /tmp/SD0/bootup.sh.  Its stock
// instaAIP web UI, however, has an unauthenticated /upload_file handler that
// writes a multipart filename under /tmp/SD0/DCIM/ without sanitising it.
// Bootstrap installs this library in /usr/lib and puts its path in
// /etc/ld.so.preload; after the next boot every dynamically linked process
// loads it.
//
// The constructor is deliberately conservative:
//   - no libc calls or dynamic dependencies;
//   - do nothing until /tmp/SD0/bootup.sh is visible (card mounted);
//   - one-shot lock in /tmp;
//   - fork, then exec /bin/sh (so the card may be mounted noexec);
//   - wait for instaAIP before running bootup.sh, matching the X5 boot order.
//
// If the SD card is absent the hook is a no-op.  Removing
// /etc/ld.so.preload and /usr/lib/libcloudbootstrap.so reverts it.

#include <stddef.h>

#define AT_FDCWD (-100)
#define F_OK 0
#define O_WRONLY 1
#define O_CREAT 0100
#define O_EXCL 0200
#define O_APPEND 02000
#define O_CLOEXEC 02000000
#define SIGCHLD 17

static const char script_path[] = "/tmp/SD0/bootup.sh";
static const char lock_path[] = "/tmp/.cloudbootstrap.started";
static const char log_path[] = "/tmp/x4preload.log";
static const char log_launch[] = "x4preload: starting /tmp/SD0/bootup.sh\n";
static const char log_fail[] = "x4preload: exec /bin/sh failed\n";
static const char sh_path[] = "/bin/sh";
static const char sh_argv0[] = "/bin/sh";
static const char sh_arg_c[] = "-c";
#ifdef X4PRELOAD_TEST
static const char sh_cmd[] = "exec /bin/sh /tmp/SD0/bootup.sh";
#else
static const char sh_cmd[] =
    "i=0; while [ $i -lt 300 ] && ! pidof instaAIP >/dev/null 2>&1; do "
    "sleep 1; i=$((i+1)); done; exec /bin/sh /tmp/SD0/bootup.sh";
#endif
static const char env_path[] = "PATH=/sbin:/bin:/usr/sbin:/usr/bin";
static const char env_home[] = "HOME=/";

#if defined(__aarch64__)
#define SYS_faccessat 48
#define SYS_openat 56
#define SYS_close 57
#define SYS_write 64
#define SYS_exit_group 94
#define SYS_clone 220
#define SYS_execve 221
#define SYS_unlinkat 35

static long sys4(long n, long a, long b, long c, long d)
{
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x8 __asm__("x8") = n;
    __asm__ volatile("svc #0" : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2), "r"(x3) : "memory", "cc");
    return x0;
}

static long sys5(long n, long a, long b, long c, long d, long e)
{
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x8 __asm__("x8") = n;
    __asm__ volatile("svc #0" : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4)
                     : "memory", "cc");
    return x0;
}

static long sys3(long n, long a, long b, long c)
{
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x8 __asm__("x8") = n;
    __asm__ volatile("svc #0" : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2) : "memory", "cc");
    return x0;
}

static long sys2(long n, long a, long b)
{
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x8 __asm__("x8") = n;
    __asm__ volatile("svc #0" : "+r"(x0)
                     : "r"(x8), "r"(x1) : "memory", "cc");
    return x0;
}

static long sys1(long n, long a)
{
    register long x0 __asm__("x0") = a;
    register long x8 __asm__("x8") = n;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8) : "memory", "cc");
    return x0;
}
#elif defined(__x86_64__)
#define SYS_faccessat 269
#define SYS_openat 257
#define SYS_close 3
#define SYS_write 1
#define SYS_exit_group 231
#define SYS_clone 56
#define SYS_execve 59
#define SYS_unlinkat 263

static long sys4(long n, long a, long b, long c, long d)
{
    register long rax __asm__("rax") = n;
    register long rdi __asm__("rdi") = a;
    register long rsi __asm__("rsi") = b;
    register long rdx __asm__("rdx") = c;
    register long r10 __asm__("r10") = d;
    __asm__ volatile("syscall" : "+r"(rax)
                     : "r"(rdi), "r"(rsi), "r"(rdx), "r"(r10)
                     : "rcx", "r11", "memory");
    return rax;
}

static long sys5(long n, long a, long b, long c, long d, long e)
{
    register long rax __asm__("rax") = n;
    register long rdi __asm__("rdi") = a;
    register long rsi __asm__("rsi") = b;
    register long rdx __asm__("rdx") = c;
    register long r10 __asm__("r10") = d;
    register long r8 __asm__("r8") = e;
    __asm__ volatile("syscall" : "+r"(rax)
                     : "r"(rdi), "r"(rsi), "r"(rdx), "r"(r10), "r"(r8)
                     : "rcx", "r11", "memory");
    return rax;
}

static long sys3(long n, long a, long b, long c)
{
    register long rax __asm__("rax") = n;
    register long rdi __asm__("rdi") = a;
    register long rsi __asm__("rsi") = b;
    register long rdx __asm__("rdx") = c;
    __asm__ volatile("syscall" : "+r"(rax)
                     : "r"(rdi), "r"(rsi), "r"(rdx)
                     : "rcx", "r11", "memory");
    return rax;
}

static long sys2(long n, long a, long b)
{
    register long rax __asm__("rax") = n;
    register long rdi __asm__("rdi") = a;
    register long rsi __asm__("rsi") = b;
    __asm__ volatile("syscall" : "+r"(rax)
                     : "r"(rdi), "r"(rsi)
                     : "rcx", "r11", "memory");
    return rax;
}

static long sys1(long n, long a)
{
    register long rax __asm__("rax") = n;
    register long rdi __asm__("rdi") = a;
    __asm__ volatile("syscall" : "+r"(rax) : "r"(rdi)
                     : "rcx", "r11", "memory");
    return rax;
}
#else
#error unsupported architecture
#endif

static void log_line(const char *msg, long len)
{
    long fd = sys4(SYS_openat, AT_FDCWD, (long)log_path,
                   O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd >= 0) {
        sys3(SYS_write, fd, (long)msg, len);
        sys1(SYS_close, fd);
    }
}

static void launch(void)
{
    char *argv[4];
    char *envp[3];
    long fd;
    long pid;

    if (sys4(SYS_faccessat, AT_FDCWD, (long)script_path, F_OK, 0) != 0)
        return;

    fd = sys4(SYS_openat, AT_FDCWD, (long)lock_path,
              O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0)
        return;
    sys1(SYS_close, fd);

    pid = sys5(SYS_clone, SIGCHLD, 0, 0, 0, 0);
    if (pid < 0) {
        sys3(SYS_unlinkat, AT_FDCWD, (long)lock_path, 0);
        return;
    }
    if (pid != 0)
        return;

    /* Do not keep the parent's sockets (instaAIP holds several) open in the
     * long-lived boot shell. */
    for (long i = 3; i < 1024; i++)
        sys1(SYS_close, i);

    log_line(log_launch, (long)(sizeof(log_launch) - 1));

    argv[0] = (char *)sh_argv0;
    argv[1] = (char *)sh_arg_c;
    argv[2] = (char *)sh_cmd;
    argv[3] = NULL;
    envp[0] = (char *)env_path;
    envp[1] = (char *)env_home;
    envp[2] = NULL;

    sys3(SYS_execve, (long)sh_path, (long)argv, (long)envp);
    log_line(log_fail, (long)(sizeof(log_fail) - 1));
    sys3(SYS_unlinkat, AT_FDCWD, (long)lock_path, 0);
    sys1(SYS_exit_group, 127);
    for (;;) {
    }
}

__attribute__((constructor)) static void cloudbootstrap_init(void)
{
    launch();
}
