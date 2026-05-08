---
name: migrate-nuttx-pinmap
description: Migrate a PX4 board from the NuttX legacy STM32 pinmap to the new non-legacy pinmap. Use this skill when the user wants to disable CONFIG_STM32_USE_LEGACY_PINMAP, CONFIG_STM32F7_USE_LEGACY_PINMAP (or the H7/L4/etc. equivalent) for a board, needs to update board.h and board_config.h pin definitions to use new-style GPIO names with correct GPIO_SPEED_xxx values, or is preparing a board for a NuttX submodule upgrade that removes legacy pinmap support. Also triggers on: "migrate pinmap", "remove legacy pinmap", "update GPIO speed", "pinmap migration", "STM32F7_USE_LEGACY_PINMAP", "STM32_USE_LEGACY_PINMAP".
argument-hint: "[board path, e.g. boards/px4/fmu-v5]"
allowed-tools: Bash, Read, Edit, Grep, Glob
---

# NuttX Legacy Pinmap Migration

Migrates a PX4 board's pin definitions from NuttX legacy-style GPIO names (which
bundle `GPIO_SPEED_xxx` inside the pinmap file) to new-style names (which have no
embedded speed, requiring the board to supply it explicitly in `board.h`).

## Background

NuttX introduced `CONFIG_STM32Xx_USE_LEGACY_PINMAP` as a compatibility shim.
When disabled, the pinmap file no longer provides speed-baked definitions. The
board must now explicitly OR in the speed in its own header. Eventually NuttX will
remove the legacy files entirely, breaking any board that hasn't migrated.

**Prefix conventions by family:**
- STM32F7, STM32H7: new pinmap uses `n_GPIO_` prefix (e.g., `n_GPIO_USART1_RX_2`)
- STM32 (F4), STM32L4, older families: new pinmap keeps plain `GPIO_` prefix,
  just without `GPIO_SPEED_xxx` in the values

**The `x_GPIO` trick (the heart of this skill)**:
Temporarily rename ALL defined GPIO symbols in the new (non-legacy) pinmap to
`x_GPIO_`. This turns every un-migrated reference in board.h into a compile error,
making gaps impossible to miss. The build becomes your exhaustive test harness.
After migration is verified, optionally rename `x_GPIO_` back to the real names.

## Step 1 — Identify board and chip variant

If the user provided a board path in `$ARGUMENTS`, use that. Otherwise ask.

```bash
grep "CONFIG_ARCH_CHIP_STM32\|STM32F7_STM32F\|STM32H7_STM32H\|STM32L4_STM32L" \
  <board>/nuttx-config/nsh/defconfig | head -5
```

From the result, derive which new pinmap file applies. Common mappings:

| Kconfig symbol | Family dir | New pinmap file |
|---|---|---|
| `STM32F7_STM32F76XX` / `F77XX` | `stm32f7` | `stm32f76xx77xx_pinmap.h` |
| `STM32F7_STM32F74XX` / `F75XX` | `stm32f7` | `stm32f74xx75xx_pinmap.h` |
| `STM32F7_STM32F72XX` / `F73XX` | `stm32f7` | `stm32f72xx73xx_pinmap.h` |
| `STM32H7_STM32H7X3XX` | `stm32h7` | `stm32h7x3xx_pinmap.h` |
| `STM32H7_STM32H7X7XX` | `stm32h7` | `stm32h7x7xx_pinmap.h` |
| `ARCH_CHIP_STM32F4xxxx` (F4) | `stm32` | `stm32f40xxx_pinmap.h` |
| `ARCH_CHIP_STM32F412xx` | `stm32` | `stm32f412xx_pinmap.h` |

If uncertain, grep the `stm32_pinmap.h` selector to confirm which file is included
for your chip:
```bash
grep -A2 "STM32F76\|STM32F74\|STM32F427\|STM32H7" \
  platforms/nuttx/NuttX/nuttx/arch/arm/src/<family>/hardware/stm32_pinmap.h | head -20
```

Locate both pinmap files:
```bash
find platforms/nuttx/NuttX/nuttx/arch/arm/src -name "<pinmap>.h" -o -name "<pinmap>_legacy.h"
```

Check the exact LEGACY CONFIG option name (differs per family):
```bash
grep "USE_LEGACY_PINMAP" \
  platforms/nuttx/NuttX/nuttx/arch/arm/src/<family>/Kconfig | head -3
```

## Step 2 — Apply the `x_GPIO` marker to the new pinmap

Rename every GPIO symbol definition in the new pinmap to `x_GPIO_`. This is
temporary and intentionally breaks the build so the compiler finds every reference
that still uses the old name.

**Important**: pinmap files may use both `#define` and `#  define` (indented, used
inside `#ifdef` blocks). Plain `sed` with `[ \t]` is unreliable — use `perl`:

```bash
# For STM32F7/H7: rename n_GPIO_ → x_GPIO_
perl -i -pe 's/^(#\s*define\s+)n_GPIO_/${1}x_GPIO_/' <new_pinmap_file>

# For STM32F4 and other plain-GPIO_ families: rename only the defined symbol
# (left side of #define), not the GPIO_ALT/GPIO_AF7/... constants in the value
perl -i -pe 's/^(#\s*define\s+)GPIO_([A-Z0-9])/${1}x_GPIO_$2/' <new_pinmap_file>
```

Verify the rename worked and that values are intact:
```bash
grep "^#.*define x_GPIO_USART1_RX" <new_pinmap_file> | head -3
# Should show: #define x_GPIO_USART1_RX_1  (GPIO_ALT|GPIO_AF7|...)
#                                            ^^^^^^^^^ GPIO_ALT unchanged — good
```

## Step 3 — Scan ALL board source files for GPIO pinmap references

The legacy pinmap's bare `GPIO_XXX` names are consumed not just by `board.h` and
`board_config.h` but also by NuttX chip drivers (serial, SDIO, USB OTG) that
include `board.h` and expect certain symbols to be defined there. When those
symbols aren't explicitly in board.h, they were silently satisfied by the legacy
pinmap. After the migration, every such symbol must be explicitly defined.

Scan broadly:
```bash
# Board headers
grep -n "GPIO_" <board>/nuttx-config/include/board.h
grep -n "GPIO_" <board>/src/board_config.h

# Any other board source files that might use pinmap symbols directly
grep -rn "GPIO_SDIO\|GPIO_OTGFS\|GPIO_ADC\|GPIO_UART8\|GPIO_CAN" <board>/src/
```

**Focus on pinmux selections** — lines of the form:
```c
#define GPIO_USART1_RX   GPIO_USART1_RX_2    /* PB7 */
```
**Ignore raw GPIO bit-field definitions** — lines using `GPIO_OUTPUT|GPIO_PUSHPULL|...`
directly. Those don't reference the pinmap.

**Watch for "implicit" pins** — peripherals enabled in defconfig whose GPIO symbols
are NOT in board.h at all. The legacy pinmap provided those automatically via the
bare `GPIO_XXXXX` name matching what the driver expected. You must now add them
explicitly. Common culprits:

| Check defconfig for | Pin symbols to add to board.h |
|---|---|
| `CONFIG_STM32_UART8=y` | `GPIO_UART8_RX`, `GPIO_UART8_TX` |
| `CONFIG_STM32_SDIO=y` | `GPIO_SDIO_CK`, `GPIO_SDIO_CMD`, `GPIO_SDIO_D0`–`D3` |
| `CONFIG_STM32_OTGFS=y` | `GPIO_OTGFS_DM`, `GPIO_OTGFS_DP`, `GPIO_OTGFS_ID` |
| ADC calls in `init.c` | `GPIO_ADC1_INx` for each channel configured |

Cross-check defconfig against board.h to find these gaps before the build:
```bash
grep "CONFIG_STM32_UART8\|CONFIG_STM32_SDIO\|CONFIG_STM32_OTGFS" \
  <board>/nuttx-config/nsh/defconfig
```

## Step 4 — Look up legacy speeds for each pin

For each pin that needs migrating, look up the speed the LEGACY pinmap provided:

```bash
grep "GPIO_USART1_RX_2\|GPIO_CAN1_RX\|GPIO_SDIO_D0\|..." <legacy_pinmap_file>
```

**Common speed patterns** (always verify against the actual legacy file — they vary
by family):

| Peripheral | Typical legacy speed |
|---|---|
| UART / USART (RX, TX) | `GPIO_SPEED_100MHz` |
| UART / USART (RTS, CTS) | often no speed (check legacy!) |
| CAN (RX, TX) | `GPIO_SPEED_50MHz` |
| SPI (MISO, MOSI, SCK) | `GPIO_SPEED_50MHz` |
| I2C (SCL, SDA) | `GPIO_SPEED_50MHz` |
| SDIO / SDMMC (CK, CMD, D0–D7) | `GPIO_SPEED_50MHz` |
| USB OTG FS/HS (DM, DP, ID) | `GPIO_SPEED_100MHz` |
| Timer capture/compare/output | `GPIO_SPEED_50MHz` |
| ADC analog inputs | *no speed needed* — `ANALOG` mode ignores slew rate |

**Note on RTS/CTS**: some legacy pinmaps omit `GPIO_SPEED_xxx` for flow-control
lines. If the legacy entry has no speed, do NOT add one — match the original.

## Step 5 — Rewrite pin definitions in board.h

For each pin definition, update the symbol to `x_GPIO_` and add `| GPIO_SPEED_xxx`:

```c
/* Before */
#define GPIO_USART1_RX   GPIO_USART1_RX_2         /* PB7 */

/* After */
#define GPIO_USART1_RX   (x_GPIO_USART1_RX_2|GPIO_SPEED_100MHz)  /* PB7 */
```

For pins with no speed in the legacy pinmap (e.g., RTS/CTS on F4):
```c
#define GPIO_USART2_RTS  x_GPIO_USART2_RTS_2      /* no speed — matches legacy */
```

Key rules:
- **No spaces around `|`** — PX4 convention is `(x_GPIO_X_1|GPIO_SPEED_50MHz)`,
  not `(x_GPIO_X_1 | GPIO_SPEED_50MHz)`.
- **Always wrap speed-OR in parentheses** — `#define FOO x_GPIO_X_1|GPIO_SPEED_50MHz`
  is a macro that expands to two tokens; `(x_GPIO_X_1|GPIO_SPEED_50MHz)` is one.
  The former silently breaks in any expression context.
- **ADC pins**: just alias the `_0` form, no speed OR needed:
  `#define GPIO_ADC1_IN2  x_GPIO_ADC1_IN2_0`
- **`_0` suffix**: single-mapping pins gain this suffix in the new pinmap (legacy
  had no suffix). E.g., legacy `GPIO_SDIO_D0` → new `x_GPIO_SDIO_D0_0`.
- **Raw GPIO bit-field pins** (`GPIO_OUTPUT|GPIO_PUSHPULL|...`) in board_config.h
  are not pinmap references — leave them unchanged.

## Step 6 — Add the LEGACY_PINMAP=n line to defconfig

```bash
grep "LEGACY_PINMAP" <board>/nuttx-config/nsh/defconfig  # check if already present
```

If missing, add near the top of the Kconfig options block:
```
# CONFIG_STM32_USE_LEGACY_PINMAP is not set
```
(Use the exact CONFIG name found in Step 1 — e.g., `STM32F7_USE_LEGACY_PINMAP`,
`STM32H7_USE_LEGACY_PINMAP`, or `STM32_USE_LEGACY_PINMAP` for F4.)

## Step 7 — Build and fix remaining gaps iteratively

```bash
make <board_target>_default 2>&1 | grep -E "error:|FAILED" | head -30
```

The `x_GPIO` trick turns every missed pin into a compile error with a helpful
`did you mean 'x_GPIO_...'?` suggestion from the compiler. Work through errors
one peripheral at a time — each error tells you exactly which symbol is missing
and often even which `x_GPIO_` name to use. Repeat until the build is clean.

**Pattern for each error:**
1. Note the missing symbol (e.g., `GPIO_SDIO_D0`)
2. Find the `x_GPIO_` equivalent: `grep "x_GPIO_SDIO_D0" <new_pinmap_file>`
3. Look up its legacy speed: `grep "GPIO_SDIO_D0" <legacy_pinmap_file>`
4. Add the definition to board.h and rebuild

If errors appear about `LEGACY_PINMAP` Kconfig not being recognized, the current
NuttX submodule predates this feature — migration must wait for the NuttX bump.

## Step 8 — Revert x_GPIO → real names (optional, do this when user asks)

Rename `x_GPIO_` back to the real prefix everywhere:

```bash
# Revert the pinmap (STM32F7/H7: back to n_GPIO_; STM32F4: back to GPIO_)
perl -i -pe 's/\bx_GPIO_/n_GPIO_/g' <new_pinmap_file>          # for F7/H7
# or:
perl -i -pe 's/(#\s*define\s+)x_GPIO_/${1}GPIO_/' <new_pinmap_file>  # for F4

# Revert all board source files
grep -rl "x_GPIO_" <board>/ | xargs perl -i -pe 's/\bx_GPIO_/n_GPIO_/g'  # F7/H7
# or for F4 (bare GPIO_ is the final name):
grep -rl "x_GPIO_" <board>/ | xargs perl -i -pe 's/\bx_GPIO_/GPIO_/g'
```

Verify nothing remains:
```bash
grep -rn "x_GPIO_" <board>/ <new_pinmap_file>
```

Do a final build to confirm the clean state compiles.

## Step 9 — Commit

Use the `/commit` skill. Scope is the board name, type is `feat`:

```
feat(boards/px4/fmu-v4): migrate to non-legacy NuttX STM32 pinmap
```

Commit body should include:
- Which CONFIG option was disabled
- Brief list of peripheral groups migrated
- Reference to the NuttX upstream commit/PR that removes the legacy files (if known)
