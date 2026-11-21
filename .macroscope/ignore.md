---
ignoreTests: false
---
# GENERATED from dmc-technologies/review-rules. Do not edit here: change review-rules
# (dmc-developers) and let the sync open a pull request.
# Files Macroscope skips during code review and in Check Run Agents.
# This file REPLACES Macroscope's built-in defaults, so the defaults are reproduced
# (from docs.macroscope.com, Code Review, as vendored in pingdotgg/t3code), EXCEPT:
# - test files and test directories stay in scope, because goldens and fixtures hold
#   expected physics values (mechanical-correctness.md section 1);
# - csv, jsonl, npy, pkl and parquet stay in scope, because they can hold material,
#   load or golden data.
# One glob per line.

# ---- Macroscope defaults (DMC subset) ----
# === Vendored / dependency directories ===
**/.git/**
**/__pycache__/**
**/.pytest_cache/**
**/.mypy_cache/**
**/.ruff_cache/**
**/venv/**
**/.venv/**
**/node_modules/**
**/site-packages/**
**/.pnpm-store/**
**/__Snapshots__/**
**/__snapshots__/**
**/.agents/skills/**
**/.claude/skills/**
**/.github/skills/**
**/bower_components/**
**/jspm_packages/**
**/.next/**
**/.svelte-kit/**
**/.nuxt/**
**/.output/**
**/.vercel/**
**/.angular/**
**/vendor/**
**/_vendor/**
**/third_party/**
**/Pods/**
**/.bundle/**
# === Root-anchored ambiguous directories ===
build/**
out/**
env/**
ENV/**
# === Generated / build-output directories (match anywhere) ===
**/target/**
**/dist/**
**/generated/**
**/intermediates/**
**/generated_sources/**
**/generated-sources/**
**/generated-src/**
**/src/main/generated/**
# === Minified build output ===
**/*.min.js
**/*.min.css
**/*.bundle.js
# === Yarn PnP loader files ===
**/.pnp.cjs
**/.pnp.loader.mjs
# === Generated protobuf / codegen files ===
**/*_pb.d.ts
**/*_pb.js
**/*.pb.go
**/*_pb2.py
**/*_pb2_grpc.py
**/*_pb2.pyi
**/*.grpc.swift
**/*.pb.swift
**/*.sql.go
**/*.designer.cs
**/*.g.dart
**/*.pb.dart
**/*_pb.rb
**/*.d.ts
**/*.gen.ts
**/*.gen.tsx
**/*.gen.js
**/*.gen.jsx
# === Package manager files ===
**/go.mod
**/package.json
**/*.pbxproj
**/*.xcstrings
**/*.strings
**/*.properties
**/pom.xml
**/Package.swift
**/bun.lock
**/.eslintrc
**/.eslintignore
# === Lock / sum files ===
**/go.sum
**/package-lock.json
**/pnpm-lock.yaml
**/yarn.lock
**/Package.resolved
# === Images ===
**/*.jpg
**/*.jpeg
**/*.png
**/*.gif
**/*.svg
**/*.ico
**/*.webp
**/*.bmp
**/*.tiff
# === Fonts ===
**/*.woff
**/*.woff2
**/*.ttf
**/*.eot
**/*.otf
# === Media ===
**/*.mp3
**/*.mp4
**/*.wav
**/*.avi
**/*.mov
**/*.mkv
**/*.flac
**/*.ogg
**/*.srt
# === Archives ===
**/*.zip
**/*.tar
**/*.gz
**/*.rar
**/*.7z
**/*.bz2
# === Documents ===
**/*.pdf
**/*.doc
**/*.docx
**/*.xls
**/*.xlsx
**/*.ppt
**/*.pptx
# === Data / serialized ===
**/*.db
**/*.sqlite
**/*.sqlite3
**/*.avro
**/*.arrow
# === ML models ===
**/*.onnx
**/*.tflite
**/*.h5
**/*.safetensors
# === Compiled / binary ===
**/*.exe
**/*.dll
**/*.so
**/*.dylib
**/*.bin
**/*.pyc
**/*.class
**/*.o
**/*.a
**/*.wasm
# === Certificates / keys ===
**/*.cer
**/*.pem
**/*.p12
# === Platform-specific / non-reviewable ===
**/*.stringsdict
**/*.snap
**/*.adoc
**/*.arb
**/*.lock
**/*.po
**/*.fbx
**/*.log
**/*.xib
**/*.meta
**/*.kml
**/*.prefab
**/*.eml
**/*.grpc.reflection
**/*.js.map
# === Generated files (rules/generated.yml, docs/GENERATED-FILES.md) ===
.review-selftest/marked/**
