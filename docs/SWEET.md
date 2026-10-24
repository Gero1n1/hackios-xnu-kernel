# sweet migration status — 2026-10-23

Latest source milestone: `fd68e441f756e9614aef905a336c2948d278865f`.
Full XNU CI and all Python host suites pass: 55 Windows-tool tests, 4 userland
tests and 2 APFS tests. QEMU run 8 completed 600 guest seconds with a normal
loader return and no panic marker, but UI beyond Apple's logo is not claimed.
The four-CPU scheduler panic now has a QEMU-only timeout tolerance; the native
root cause remains unproven. No native sweet hardware feature is newly claimed.
Read the final sections below for current hashes, run IDs and the next concrete
actions.

Active worktree: `E:/hackios-sweet`, kernel branch `sweet-port`. Original
`E:/порт/ios-sweet` is preserved, including its local commits and driver work.

## Base and limits

- Kernel upstream: `oosexgang/hackios-xnu-kernel` at
  `8e61187098f2c26c5daee8f9f72b6416aa64b9ab` (APSL 2.0 plus notices).
- Userland upstream: `oosexgang/hackios-xnu-userland` at
  `906425b75e02b4f99eb5d7bb1ef7c9583472d772` (MIT plus third-party licenses).
  This is a second Git repository in `.userland.git`, sharing this worktree.
- Matched iOS version: 16.5 / 20F66 / iPhone X, XNU 8796.121.2.
  Do not mix the original port's 16.7.16 files into this baseline.
- Upstream's hardware results concern redwood (SM7325, A642L, WCN6750,
  GT9916 SPI). sweet has SM7150, A618, WCN3990/ICNSS and GT9896 I2C.
  No native sweet GPU, Wi-Fi, charging or 120 Hz result is claimed here.
- No phone boot, flash, partition write, GPU/radio startup or MMIO experiment
  has been performed. The owner takes the Android phone away overnight.

## Implemented and checked locally

1. Fixed `userland/mkjobs.py`: an environment-variable loop overwrote the
   output filename of every on-demand job with `RWA_PACKED16`. Two regression
   tests fail against unmodified upstream and pass against the fix, with the
   experiment enabled and disabled. The real image now contains 407 parseable
   launch-job plists, including 382 generated on-demand jobs.
2. Added Windows preparation/build/run adapters in `tools/windows/`:
   exact bounded HTTP 206 ranges, ZIP size/CRC checks, resumable stored/deflated
   members, NTFS ADS LZBITMAP completion, literal-name remapping, extended paths,
   virtual POSIX trees, LF boot configuration, APFS generation and bounded QEMU
   capture. No fastboot/ADB or handset-writing path exists in these adapters.
3. Added single-process APFS compression (`--jobs 1`) for the 8 GB host;
   it produces the same bytes as two workers on the test corpus. Added Windows
   positional-read fallback with binary descriptors, and CRLF manifest handling.
4. Rebuilt the EFI loader with Zig/LLVM, warnings as errors. DMA completion is
   volatile with barriers and a bounded poll. IMPORTANT: the published loader
   left ramfb at 800x600; both our unmodified-source control rebuild and changed
   rebuild reach 1125x2436. Compiler/prebuilt differences remain unresolved;
   do not claim the DMA change alone has proven the root cause.
5. `uefi/sweet-memory.patch` applies cleanly to Mu-Silicium
   `e51cd877e31c8fbb322032dc4cb6715e83d25ad3`'s sweet_k6a map. It moves the DXE
   heap start to 0x98000000 (60 MiB remain) and extends PIL reservation accordingly.
   All six overlaps of allocatable RAM with the live sweet reserved-memory nodes
   disappear, including ADSP, MSA, NPU, IPS, IPA GSI and GPU firmware. This is a
   source/map check, not a UEFI build or a handset validation. This candidate UEFI
   must not be enabled by copying the redwood phone profile.

## Evidence

- Windows tests: 18 tests in `tools/windows/tests`, 2 mkjobs regression tests,
  and 2 APFS host-writer regressions.
- App Cryptex: 884 regular files, 564 LZBITMAP completions. Rebuilt APFS reads
  all 884 files byte-identically; all 1047 paths/modes/owners match, zero bad
  object checksums. All 1049 directory-record hashes match, none out of order.
- C `apfs_format.h` check: 119 field offsets and 19 structure sizes match Python.
  All 9 existing sparse-read cases pass. This is not an apfsck certification.
- OS Cryptex: 1454 regular files, 981 LZBITMAP completions, sizes verified.
- Selected root: 43,324 regular files, all sizes checked, 12 NTFS name remaps.
  The two fonts were then trimmed using the upstream recipe. Downloaded DMGs
  had ZIP CRC verified; only our verified temporary root/OS DMGs were removed
  afterward to fit the owner's disk budget. No full IPSW was downloaded.
- QEMU static PID 1 only: `E:/hackios-qemu-ramfb-1` (75.55 host seconds,
  clean guest return); `E:/hackios-qemu-ramfb-control` (44.69 seconds, control);
  `E:/hackios-qemu-smp4-1` (124.39 seconds, four CPUs reported running).
  These are not Apple launchd/SpringBoard or physical-phone tests.

## Full images and QEMU evidence

`userland/windows-build-1.log` completed the 2,492,203,008-byte `system.apfs` in
`userland/windows-qemu-1/` (45,137 files, 16,545 directories, 672 links).
Its small `root.apfs` predates the LF correction:
**use `userland/windows-qemu-2/root.apfs` instead**. That corrected small image
is built and read back: rc, rc.jobs, var.skel and inject.conf contain LF only;
all 407 launch jobs parse as plists. Critical launchd, dyld and SpringBoard
executables read back byte-identically. This is not a complete APFS filesystem
certification. Userland fixes are committed as `7c73287e8f3be7905d17d994db43f2b68df75db8`.

The first full run used:

```text
python tools/windows/run_qemu.py --qemu <qemu-system-aarch64.exe>
  --efi host-tools/qemu-firmware/share/edk2-aarch64-code.fd
  --vars host-tools/qemu-firmware/share/edk2-arm-vars.fd
  --loader xnuloader/loader.efi --ramdisk userland/windows-qemu-2/root.apfs
  --system userland/windows-qemu-1/system.apfs --memory-gib 7 --cpus 2
  --network --seconds 600 --output E:/hackios-qemu-ios165-1
```

Use a **new ASCII output directory**. Windows QEMU lacks memory-backend-file;
the runner preloads the system directly and does not create a 12 GiB ram.img.
Keep screenshots, console and exact input hashes. `--network` is an emulated
virtio NIC behind QEMU NAT, not WCN3990 Wi-Fi. Investigate actual boot failures
before changing more drivers.

First full run: `E:/hackios-qemu-ios165-1`, 641.19 host seconds, 2 guest CPUs.
Apple launchd, SpringBoard and backboardd ran; backboardd logged startup complete.
Initial checks: 36, zero failed; IOSurface: 80, zero failed. The visible screen
remained at Apple's boot logo; the lock screen is NOT validated. The kernel
returned normally to the loader at its requested time; QEMU itself was then
terminated by the host deadline (Windows exit code 1 is not a guest panic).
The loader replays its log on return: repeated lines are not a second boot.
Repeated lsd CPU-monitor entitlement warnings have not been proven causal;
the genuine executable does not have the override entitlement. No workaround
granting permissions or disabling CPU monitoring has been added.

The second run `E:/hackios-qemu-ios165-2` was interrupted on 22 October before
its final report; it is not a completed 1200-second run. It reports four
processors running and records SpringBoard stacks at guest uptime 123.590
and 447.241 seconds, lsd at 651.347 seconds. The main SpringBoard chain is
unchanged across those two samples. The final captured screen is the Apple
logo, not a lock-screen pass. Current runner stores native PNG
snapshots and all-CPU registers, distinguishes host termination from guest exit,
and offers a separate local QMP control port (`--control-port 4446`). QMP input
has successfully requested live SpringBoard stacks and per-process CPU usage.
No lock-screen result yet. Host tests total 22, all passing.

## Source-build CI

`.github/workflows/linux-xnu.yml` builds on Ubuntu 24.04, using a pinned Theos
16.5 SDK commit and a tagged Linux cctools archive (hash recorded in artifacts).
It keeps the actual make exit status, including upstream export-check failures;
an available linked kernel does not make a failed CI build a success. Evidence
contains logs, source/compiler IDs and any linked kernel, never Apple images.
No completed new kernel build result is claimed at this point.

The first dispatched job `39828398981` fetched SDK/cctools and applied XNU,
then failed at host-tool compilation because Ubuntu lacked `uuid/uuid.h`.
Added `uuid-dev`; this is a dependency fix, not a completed kernel build.
Run `39828874995` subsequently compiled and linked
`mach.development.bcm2837.unstripped`, including the new board-identity guards.
It then failed before final kernel publication: `dsymutil` was missing,
leaving `--minimize: command not found`. Added the Ubuntu `llvm` package and
an explicit tool-presence check. Logs downloaded to
`host-tools/ci-39828874995/`; this run is a failed build, not a tested kernel.

Run `39897654980` produced a final 15,160,872-byte ARM64 kernel, SHA-256
`b7dab25a7bde08a5e2344c9a995f5a43230065629538b7acf5b4126d43c2b3ff`,
at source `76735ab`. Make then failed its symbol-set checks: the generic port
advertised unavailable generated DriverKit RPC and Apple PPL/DART interfaces.
The earlier upstream claim that all failures were DriverKit-only is inaccurate.
An explicit source patch removes 131 unavailable export entries (89 IOKit,
2 Libkern, 35 Private.arm64, 5 Private); it adds no success-returning stubs and
does not disable export/symbol-set checking. Applied/reversed cleanly against
Apple `c54f35ca767986246321eb901baf8f5ff7923f6a`. This is the generic port's
supported KPI list, not a patch intended for an actual Apple hardware build.
The new full CI result is still pending.

Run `39898735749` confirms the missing-export failures are gone and all seven
symbol sets are generated. Its remaining failure is host portability in
`check_all_exports`: the symbol-set tool cannot write its output to `/dev/null`
(`couldn't write output: Success`). Changed both checks to write separate
regular files in their existing build directory. Both checks and their errors
remain active. The follow-up full build result is pending.

## 23 October: boot wait chain

Downloaded/extracted the 591,855,616-byte shared-cache symbol member from the
same CRC-verified iOS 16.5 OS DMG; symbols SHA-256
`791bcbc49a7d175d5625f927f0157cece242aeb46d53da0f22f8c7a1aa17bf51`.
Only the verified temporary DMG was deleted afterward. Resolved 72 addresses
from run 2 using ipsw 3.1.739. SpringBoard waits in LaunchServices database
initialization; another SpringBoard thread waits for database reseeding via
XPC; one lsd thread waits for a container query. These are observed stacks,
not proof that the lsd CPU-monitor warning is the cause.

Run 3 (`E:/hackios-qemu-ios165-3`) uses the newly built kernel and matching
`loader-platform.efi`; it has reached Apple launchd. A live container-manager
sample at uptime 92.911 seconds waits for log preferences through firehose;
at 221.934 seconds that process has progressed to 5 threads. Therefore that
first logging wait is not established as a permanent deadlock. The full run
and lock-screen result are still pending. Board-identity C tests also pass
under Zig's undefined-behavior sanitizer, exit 0 with no diagnostics; all 22
Python host tests passed again on 23 October.

Final run-3 result: 643.86 host seconds, initial 36 checks and IOSurface 80
checks passed, but a real kernel panic occurred at about 510 guest seconds.
Ticket lock `pset0 + 0x100` timed out after 84,354,243 ticks (limit 60,000,000)
in `sched_clutch_thread_update_scan` / `sched_timeshare_maintenance_continue`,
with four CPUs. The loader return includes this panic and is not a clean
boot result. Host termination afterward has exit 1. No timeout was relaxed.
Host descheduling/page pressure is a hypothesis, not an established cause.
The 4-MiB diagnostic image `windows-qemu-diag-1/root.apfs` has been built and
its selected job OS_ACTIVITY_DT_MODE settings read back from APFS.

## Passing source build and bounded Windows RAM streaming

Full source-build run `39899318652` **succeeded**, including symbol-set/export
checks, at `6a4a8138ac960da03d0f2c5880c79c552b95e7eb`. Downloaded final kernel:
15,144,488 bytes, SHA-256
`9f6e2885399619977b37c2d1ad1ce0872077e06df6b7e7d2aa946ef9a3cafef7`.
This is compilation/link/validation evidence, not a sweet hardware result.

Run 4 could not start the guest: Windows QEMU's raw loader requested an extra
4,294,967,296 bytes to read the 2.49-GB image and failed allocation. Exit 1,
42.97 host seconds; no guest checks ran. The new default streams the image
through QEMU's loopback-only GDB physical-memory mode while CPUs are paused,
then checks the streamed SHA-256 and three RAM samples before detaching.
The old ROM-copy path remains an explicit `--system-transfer loader` fallback.
No raw backing-file duplicate is made. Startup has a separate bounded budget;
progress.json remains explicitly incomplete while loading/running.

Actual 1-MiB RAM smoke: 0.094 seconds, exact input hash and 3 readbacks, 64-MiB
test VM. Full run 5 (`E:/hackios-qemu-ios165-5`) subsequently transferred all
2,492,203,008 bytes, SHA-256
`855e2372d171a80bdb19dfe35925f7a4bc7bae04b94304a2956b8f944afe78b1`,
and passed all 3 RAM samples. The kernel started at 265.86 host seconds. It
uses 1 CPU, the passing CI kernel above, and the targeted diagnostic boot
image; final runtime/lock-screen result is still pending. This is a diagnostic
configuration change, not proof that one variable caused previous failures.

Current Python host tests: 30 Windows-adapter tests plus 2 mkjobs and 2 APFS
regressions. The new workflow checks them on Ubuntu and Windows with the
source-only userland companion pinned at `7c73287`. No Apple image is used.

## GPU firmware bounds

The inherited block-format GMU loader accepted incomplete final headers,
ignored non-word-aligned tail bytes, used wrapping 32-bit payload ends and
treated endpoints in different TCM regions as one valid MMIO range. Added
full-input validation before allocation/upload and same-region containment.
Malformed input is rejected; zero-size/excess/wrapping memory requests are
rejected too. C11/C++14 fixtures cover truncation, alignment, address wrap,
TCM boundaries, metadata and a bounded malformed corpus. CI additionally runs
AddressSanitizer/UBSan and rebuilds the kernel. These are block-format checks;
A618's flat ITCM firmware still requires its separate startup implementation.

Full XNU build with these GMU checks succeeded in run `39900702926` at
`fd68e441f756e9614aef905a336c2948d278865f`: final kernel 15,144,488 bytes,
SHA-256 `a04755089dca3cbf91af5c793a631cb5af5aa6aa7cd4fb34c055d7e75762bb48`.
Downloaded/verified in `host-tools/ci-39900702926/`; this latest binary has not
yet been booted. Both host workflow runs `39900707736` / `39900703070` passed;
each has Windows and Ubuntu jobs with all 34 Python tests passing, no skips.
Ubuntu additionally passed native C/C++ and AddressSanitizer/UBSan GMU tests.
The GMU fixture also passed Windows Zig UBSan locally, exit 0, no diagnostics.

## Current handoff after run 5

Run 5 finished in 906.81 host seconds: 265.86 seconds through streamed preload
and kernel start, then the requested 600-second guest session and host cleanup.
Loader returned, no early exception/panic marker, no log-limit hit; initial
36 and IOSurface 80 checks passed. QEMU was deliberately terminated afterward
at its host deadline (exit 1); this is not a guest crash. Screen remains at
Apple's logo, so a lock-screen/home-screen result is not claimed.

Targeted logs show lsd successfully creating application containers, with
UNSUPPORTED errors for data-protection operations. At guest uptime 474.974,
SpringBoard still waited for LaunchServices; lsd had running registration
threads at 475.307. Use `rg -a` on this log because some Apple os_log output
contains NUL bytes. Do not infer a permanent container/logging deadlock from
an early sample, and do not hide unsupported data protection with fake success.

Next: investigate initial LaunchServices registration to reach the real UI.
A longer bounded single-CPU run or an explicitly labelled diagnostic
comparison of on-demand job policies can distinguish slow initial reseeding
from a dependency that never finishes. Keep the normal recipe's services
intact and do not call a reduced diagnostic image a usable system. The
four-CPU `pset0 + 0x100` ticket-lock panic now has a QEMU-only timeout
tolerance and a clean four-CPU run 8; host descheduling remains a hypothesis,
not a proven guest scheduler defect. Native A618/WCN3990/charging/panel work
still requires target-specific implementation and future sweet checks.

## Clean-boot diagnostic (run 9)

`tools/windows/build_images.py` now has `--no-redwood-jobs`, which omits
`com.redwood.demo`, `com.redwood.wifi` and `com.redwood.varstore`. This removes
the diagnostic `cpu_test 240 &` from the boot without removing any Apple
service. The resulting image `userland/windows-qemu-3/root.apfs` has SHA-256
`f8d29ec969ab09187f12f1ee6b34ab7f6d07518e7583d5d0b2c84bca47c94b98`; the
LaunchDaemons readback contains no `com.redwood.*` job.

Run 9 (`E:/hackios-qemu-ios165-9`) used 4 CPUs, 7 GiB, the same kernel, system
image and fixed loader as run 8, and 600 guest seconds. Kernel start was at
322.91 host seconds; the run ended at 964.14 host seconds with the loader
returned, no panic marker and no early exception. The initial 36 checks passed.
The final screenshot is still Apple's logo, so the lock screen is still not
validated. `lsd` logged the CPU-monitor denial 317 times, and SpringBoard's
constructor logged the activation/setup-assistant and battery hooks, but no
Siri-orb prewarm line appeared: SpringBoard still has not reached
`applicationDidFinishLaunching`. Removing the diagnostic CPU stress did not
change the LaunchServices wait within 600 seconds.

## Run 10: lsd progress and the effective ticket timeout

`userland/windows-qemu-4/root.apfs` has SHA-256
`935e44b3611e7ea1cf689547ff97316a9f1a7823332efb62a990747b0bf0c7e9`; it is the
run 9 clean root with `OS_ACTIVITY_DT_MODE` on `lsd` and no `com.redwood.*`
jobs. Run 10 (`E:/hackios-qemu-ios165-10`) used 4 CPUs, 6 GiB, the same
kernel/system/loader, a QMP control port and 900 guest seconds. It ended at
1242.88 host seconds (kernel start 301.92) on the host deadline; the loader
returned, the initial 36 checks passed, and the panic marker was set. The
final screenshot is still Apple's logo.

The stack samples show progress, not a proven deadlock. At lsd uptime 321.268
thread 0x1b1 had one stack; at 627.780 it had a different stack and lsd had
grown from 7 to 8 threads. `top` reported lsd at 20.6% of the processor at
325 s and 59.1% at 627 s. `lsd` was creating plugin data containers
(`makeContainerLiveReplacingContainer`) and logged the existing
`container_set_data_protection... UNSUPPORTED` failure for each; it was still
working when the panic cut the run off. SpringBoard's stack was identical at
323.598 and 630.084 and still waited for LaunchServices; no Siri-orb line or
lock screen appeared. The planned third stack sample produced no stack
because the panic intervened.

The panic is the QEMU ticket-lock timeout, not an lsd crash: CPU 2, task lsd
pid 24, thread tid 1452, lock `0xfffffff9002a1c20`, cticket `0xfb`,
nticket `0xfc`, waiter `0x7a`, owner 0 on CPU -1, elapsed 62,391,000 ticks
against `timeout: 60000000`. The backtrace was
`hw_lck_ticket_timeout_panic` ← `hw_spin_should_keep_spinning` ←
`hw_lck_ticket_contended` ← `lck_ticket_contended` ←
`workq_unpark_continue+0x3c` ← `Call_continuation`. The timeout value is
exactly the 20x global scale: the loader's `ticket-lock-scale=10` property is
not consumed by this kernel. The QEMU runner now adds `tlto_us=25000000` to
the boot arguments; `ml_init_lock_timeout()` reads it and sets the ticket
timeout to 25 s. The loader property is kept for a kernel that implements it,
but it was inert here. This is QEMU-only; the redwood/sweet profile has no
such boot arg and keeps the default.

The next run uses the same clean root, `tlto_us=25000000`, 1200 guest seconds,
6 GiB, `--host-grace 180` and the QMP control port, to see whether lsd finishes
the initial seeding and BootUI leaves the Apple logo. No lock-screen claim is
made from run 10.

## Run 11: full kernel budget without the ticket-lock panic; first SpringBoard UI steps at the cut

Run 11 (`E:/hackios-qemu-ios165-11`) was the first run of the
`tlto_us=25000000` boot argument plus `--host-grace 180`: 4 CPUs, 6 GiB, the
same clean root, kernel, system image and loader as run 10, control port 4447,
1200 seconds. It ended on the host deadline at 1691.55 host seconds (kernel
start 310.84), the loader returned, the initial 36 checks passed, and the
panic marker was false. The run 10 panic signature (ticket-lock timeout on a
`workq_unpark_continue` continuation) did not recur. One clean run does not
claim that spinning can never time out in another shape; it records only that
no panic occurred within this budget.

The console is two streams: the live serial flow (boot to about 00:30 guest
time) followed by the kernel log the loader returned, whose 1,359,805-byte
buffer stopped accepting entries at 00:13:52; counts must come from the live
flow or they double-count. `lsd` finished the extension-container phase of
initial LaunchServices seeding: 682 `makeContainerLive` plugin data containers
with 661 distinct bundle identifiers (426 in run 10), one to two host seconds
per container, and the phase closed with
`-[MILaunchServicesDatabaseGatherer performGatherWithError:]: Updated app
extension data containers with parent bundle IDs` at 00:25:51, about 13.7
guest minutes after it began at 00:12:08.

SpringBoard's launch-services wait then ended. In the final seconds of the
budget the live log shows the first SpringBoard UI activity: orientation
events requested from backboardd (00:29:35), BootUI still sustaining the boot
logo for pid 8 (00:29:49), `Unbalanced calls to begin/end appearance
transitions for <SBCoverSheetPrimarySlidingViewController>` (00:29:59) and
`UISensor ... reason: SB + backlight-off + pocketTouches;
proximityDetectionMode: locked` (00:29:59). The screen at the cut is still the
Apple logo and the surface checks remain unpassed, but the ambiguously
logging launch hook lines like the Siri-orb prewarm are not the only evidence:
the cover-sheet object and the sensor mode are SpringBoard UI code running.
CoreMotion could not set orientation properties (`fHidDeviceInterface NULL`,
`No reply for LastOrientation`, orientation resolved to unknown): expected on
QEMU with no accelerometer, recorded as a gap.

Symbolicated run 11 stacks (`qemu/symstack.py` with the exact extracted
16.5 shared cache) mark the transition precisely. At uptime 323.013, 629.540
and 936.033 SpringBoard's main thread was blocked in
`-[CCSModuleRepository _queue_filterModuleMetadataByAssociatedBundleAvailability:]`
← `LSApplicationRecord initWithBundleIdentifier` ← LaunchServices database
context, and another thread waited in `LSCopyServerStore` under
`___LAUNCH_SERVICES_IS_RESEEDING_ITS_DATABASE_AND_MAY_BLOCK__`; identical
stacks until seeding ended. At uptime 1142.565 SpringBoard had 22 threads, the
main thread was running `-[SBHIconManager _precacheDataForRootIcons]`
← `UIImage imageNamed` asset loading, a `com.apple.uikit.eventfetch-thread`
existed, `SBWorkspaceKeyboardFocusController` set up the keyboard arbiter, and
CoreMotion had a `MotionThread`. This documents the wait ending and real UI
work starting; it does not claim which UI object became visible.

New measurement notes for the next run: repeating
`Failed to copy activation record ... "Device is not activated"` lines from
lsd are the known activation gap; `kpersona_find_by_type()` fails with errno 3
313 times and `com.apple.wifi.manager` lookup fails 26 times, recorded, not
fixed here. Guest userland timestamps run roughly 0.8 times host speed
(run 10 about 0.81, run 11 about 0.85), like run 10.

Run 12 repeats run 11 with a 3600-second budget, because the first SpringBoard
UI steps began right at the 1200-second cut. `tools/windows/run_qemu.py`
previously rejected `--seconds` above 1800; its ceiling is now 7200 with a unit
test, because long TCG boots are a deliberate measurement need.

Rechecked both upstream main refs on 23 October: kernel remains `8e61187`,
userland `906425b`; no unseen upstream update was incorporated or assumed.
App heartbeat `ios-sweet` is ACTIVE, configured for four hourly continuations
of this same thread. Inspect/update it rather than creating another one.
No phone action is permitted by the saved continuation prompt.

Draft review branches: kernel <https://github.com/Gero1n1/hackios-xnu-kernel/pull/1>,
userland <https://github.com/Gero1n1/hackios-xnu-userland/pull/1>.

## Preventing redwood hardware sequences on sweet

The loader now emits `/chosen/hackios-platform` from its selected hardware
profile (separate from Apple's userland device identity). GPU, Wi-Fi and ADSP
initialization require exact `redwood` plus the existing peripheral-base check.
The GPU guard also covers `adreno=N` and `display_probe`, previously able to
bypass automatic-start checks. The radio's Linux-KPI-without-radio experiment
is confined to `qemu-virt`. Old loaders missing this property leave these
drivers disabled: use a matching new loader with a rebuilt kernel.

Thirteen identity assertions pass as C11 and C++14 on Windows, including absent,
NULL, truncated, non-NUL-terminated and wrong-board properties. The new loader
compiled with warnings as errors, SHA-256
`3da5dc9b8f37ad225fde299c1d35bf2b966fa2c99157f8343e23f3da76412bcf`.
This isolates three inherited hardware paths; it does not implement sweet's
A618/WCN3990/PM6150 startup or certify other inherited drivers for sweet.

The original ios-sweet 0088 kernel CI run `39812559222` ended without running
build steps: GitHub annotations say the hosted runner could not acquire the
job and cite macOS ARM64 capacity constraints. This is not a successful build
or a source compilation failure. Last verified original-port kernel is 0087.

## 23 October: four-CPU pset ticket-lock timeout

Run 3 (`-smp 4`, kernel `ci-39897654980`, SHA-256
`b7dab25a7bde08a5e2344c9a995f5a43230065629538b7acf5b4126d43c2b3ff`,
loader `3da5dc9b8f37ad225fde299c1d35bf2b966fa2c99157f8343e23f3da76412bcf`)
panicked in `hw_lck_ticket_timeout_panic` on `pset0 + 0x100` after
84,354,243 ticks against a 60,000,000-tick limit. The ticket state was
`cticket=0x38`, `nticket=0x3c`, waiter `0x3a`, advisory owner 0 (CPU -1).
The backtrace was `hw_lck_ticket_timeout_panic` ←
`hw_spin_should_keep_spinning` ← `hw_lck_ticket_contended` ←
`lck_ticket_contended` ← `sched_clutch_thread_update_scan+0x60` ←
`sched_timeshare_maintenance_continue+0xe4` ← `Call_continuation`.

The pset lock is a `lck_ticket_t`. Apple's `lock_ticket.c` documents that the
owner is advisory: it is recorded after the ticket is acquired and cleared
before `cticket` advances. The timeout is measured with `ml_get_timebase`, not
the owner's CPU time. The pre-panic snapshot `00500.2.txt` shows waiters but no
coherent pset critical section; `smp-review.md` therefore does not claim a
proven guest scheduler deadlock. The evidence is consistent with a QEMU MTTCG
host vCPU being descheduled in one of those short windows; it does not prove
that either.

QEMU-only tolerance: the loader already emitted
`/machine-timeouts/global-scale=20` for multi-core QEMU and later also emitted
`/machine-timeouts/ticket-lock-scale=10`. Run 10 shows that this kernel does
not consume the ticket-lock scale: its panic reports `timeout: 60000000`, the
20x value, not the intended 200x. `tools/windows/run_qemu.py` now adds
`tlto_us=25000000` to the kernel boot arguments, which
`ml_init_lock_timeout()` reads and uses to set the ticket timeout to 25 s.
The redwood/sweet profile emits neither scale and gets no such boot arg, so
real hardware keeps XNU's default 0.125 s ticket-lock timeout. This is a
bounded emulator tolerance, not a scheduler fix and not evidence that sweet
hardware is unaffected.

The rebuilt loader is `host-tools/loader-platform.efi`, SHA-256
`bed924d8dc0d5195c4e630956af55f7d9bb554a6e34947e9bd273c87ac1bc34e`.
Its source comment was later corrected to say that `ticket-lock-scale` is not
consumed by this kernel; the comment-only edit does not change the binary (its
hash above is unchanged).

Verification: run 8 (`E:/hackios-qemu-ios165-8`) used the same kernel, root
and system hashes as run 3, four CPUs and the rebuilt loader. It completed
600 guest seconds in 950.17 host seconds
(kernel start at 309.09 host seconds), returned the loader normally, and
reported no panic marker or early exception. The initial 36 and IOSurface 80
checks passed. QEMU was terminated at its host deadline (exit 1), not by a
guest crash. The host was under memory pressure: QEMU's private commit was
about 7.4 GiB on a 7.8 GiB machine, so this also exercised host descheduling.
Run 8 streamed the system image over GDB instead of run 3's raw loader, so it
is not a byte-for-byte controlled comparison. It confirms that the previous
84M-tick timeout no longer fires; it does not prove a guest scheduler fix or
any native sweet behavior.
