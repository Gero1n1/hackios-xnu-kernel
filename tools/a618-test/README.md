# A618 firmware host fixtures

These fixtures exercise the exact inline implementation used by the kernel's
`adreno618_gmu_upload()` binding. Registers are inert process RAM; Device Tree
queries are fixture functions. No GPU command or firmware is executed.

From the repository root, with a C11 or C++14 compiler:

```sh
cc -std=c11 -Wall -Wextra -Werror -Itools/platform-test/include -Ixnu/files/bsd tools/a618-test/test.c -o a618-test
c++ -std=c++14 -Wall -Wextra -Werror -Itools/platform-test/include -Ixnu/files/bsd tools/a618-test/test.c -o a618-test-cxx
./a618-test
./a618-test-cxx
```

Run with `-fsanitize=address,undefined -fno-sanitize-recover=all` where supported.
Two optional local firmware arguments check the owner's flat GMU image and
SQE slicing against inert RAM: `a618-test /private/a618_gmu.bin /private/a630_sqe.fw`.
Neither firmware file belongs in the repository.

The compiled XNU entry point lives in `xnu/files/bsd/dev/redwood_adreno.c` and
has no automatic startup caller. It requires an exact `sweet` loader identity,
an A618 chip ID and an already accessible GMU mapping at `0x0506a000`. Physical
mapping correctness, CX rails/clocks and startup serialization remain caller
responsibilities. Bounds/reset checks cannot make unpowered MMIO safe.

The flat firmware is copied while CM3 reset remains asserted, with optional
readback. No reset release, HFI, SMMU, zap authentication, submission, interrupt,
fence, IOSurface or graphics API implementation is provided by this component.
The existing redwood initialization guard and hardware sequence remain separate.

SQE's common four-byte file header is also handled by the existing redwood
core-buffer allocation path, now rejecting non-word-aligned firmware lengths
before allocating/copying code. The caller's maximum file size is a safety
allocation bound, not a claimed A618 hardware limit.
