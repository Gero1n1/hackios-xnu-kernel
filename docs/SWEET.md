# sweet migration status — 2026-10-22

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

The second run `E:/hackios-qemu-ios165-2` is in progress with 4 CPUs and 1200
seconds. It reports four processors running. Current runner stores native PNG
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
