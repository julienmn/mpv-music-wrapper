# Cover Selection Specification

## Inputs per candidate

- Dimensions: width, height
- Filename tokens (normalized, no extension)
- Front keywords: `front`, `cover`, `folder`
- Non-front keywords: `back`, `tray`, `cd`, `disc`, `inlay`, `inlet`,
  `insert`, `booklet`, `book`, `spine`, `rear`, `inside`, `tracklisting`
- Album-name tokens
- Scope (proximity):
  - **Single-disc album (priority)**:
    1. embedded
    2. track folder (album root)
  - **Multi-disc album (priority)**:
    1. embedded
    2. track folder (same disc)
    3. album-root / other non-disc folders (e.g., scans, artwork)
    4. other-disc folder

## Definitions

- Squarish: aspect ratio between 0.9 and 1.1 (inclusive).
- Album-name overlap ratio: (matching filename tokens ÷ album-name tokens),
  capped at 1.0.

## Bucket assignment

1. **Bucket 1 (front)** if all are true:
   - Squarish.
   - No non-front tokens, except if that token is also in album-name tokens.
   - And either:
     - Has a front keyword, or
     - Album-name overlap ratio >= 0.75.
2. **Bucket 2**: all other candidates.

## Selection

1. If Bucket 1 is non-empty:
   - Prefer larger resolution/area within Bucket 1.
   - If sizes are comparable, prefer better scope (use the priority above).
2. If Bucket 1 is empty:
   - Choose from Bucket 2 using the same scope ordering with existing
     area/name-token tie-breakers.

## Tiny handling

If Bucket 1 has only tiny images and Bucket 2 has any non-tiny images, pick the best non-tiny image in Bucket 2; otherwise pick the best in Bucket 1.
