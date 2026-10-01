# pdfmergepy — Session Status Summary

> 供下一个 session 直接接手，无需重新阅读历史对话。

---

## 项目定位

`https://github.com/garyliu-seismic/pdfmerge-py` — CTS2.0 itext7 PDF merge 功能的 Python POC 重写。  
底层库：**pikepdf ≥ 9.0**（基于 QPDF，MPL-2.0）。  
对标源码：`C:/project_new/content-transformation-service-v2/src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/`

```
editable install : pip install -e .
测试命令         : python -m pytest tests/ --ignore=tests/_proto_tagtree.py -q
当前结果         : 167 passed, 0 failed
```

---

## 模块清单（全部已实现）

| 文件 | 行数 | 功能 | 对标 CTS2.0 |
|------|------|------|------------|
| `merge.py` | ~70 | plain concat：AcroForm + tag tree per-page | `CustomMerger.Merge` plain 路径 |
| `composite.py` | ~660 | Form XObject 矩阵叠合（Mode A / B） | `MergePDFToUseExternalPageSize` / `MergePDFToFitMainPdfSize` |
| `mergeinfo.py` | ~220 | WorkspaceMergeInfo `<PDFMerge>` XML 解析 + 路径解析 | `ExternalPDFMergeInfo` 序列化层 |
| `partial.py` | ~410 | **Partial PDF merge（`<partialPDF>` 路径）** | `PartialPdfMerger` / `PdfPageRectangle` |
| `pdfutil.py` | ~75 | 页面范围解析 / page_geometry inspect | 工具层 |
| `acroform.py` | ~145 | AcroForm 字段复制 + 冲突重命名 + DR 合并 | `PdfPageFormCopier` / `CustomCopier` |
| `tagtree.py` | ~1000 | StructTree plain-copy + XObject MCR 迁移 | `PdfMergeTagHelper` |
| `cli.py` | ~160 | CLI（`merge` / `info` / `merge-xml` / `partial-merge`） | — |

---

## CLI 用法

```sh
# 合并整份 PDF
pdfmergepy merge a.pdf b.pdf -o out.pdf

# 合并指定页码（1-based，支持范围和逗号）
pdfmergepy merge a.pdf:1-3 b.pdf:2,4-5 -o out.pdf

# 检查页面几何（与 itext7 输出 diff 用）
pdfmergepy info out.pdf

# 按 WorkspaceMergeInfo <PDFMerge> XML 合并（全页替换/叠合）
pdfmergepy merge-xml merge.xml --main main.pdf --inputs-dir blobs/ -o out.pdf

# 按 WorkspaceMergeInfo <partialPDF> XML 合并（子区域嵌入）
pdfmergepy partial-merge merge.xml --main main.pdf --inputs-dir blobs/ -o out.pdf
```

---

## 测试覆盖

| 测试文件 | 用例数 | 覆盖点 |
|---------|--------|-------|
| `test_merge.py` | 7 | 页面范围解析、plain concat、页面属性保留 |
| `test_mergeinfo.py` | 17 | XML 解析（direct/ApplySlide）、路径解析、矩阵数学、Mode A/B 集成、Form XObject resources |
| `test_acroform.py` | 11 | AcroForm 字段复制、名称冲突重命名、嵌套 parent/kids、/DR 合并、Mode A&B |
| `test_tagtree.py` | 16 | StructTree 结构、RoleMap、ParentTree slot、/Pg 引用、PDF/UA metadata、mixed tagged/plain |
| `test_partial.py` | **21** | partialPDF XML 解析、4种 content-control、CropBox+Rotate 矩阵、end-to-end overlay、z-index 多层、hidden slide、真实 XML round-trip |

`tests/_proto_tagtree.py`：原型脚本，已排除在 pytest 之外。

---

## partial.py — 新模块详解（本 session 新增）

### 对标 C# 类

| Python 函数 | C# 对标 | 说明 |
|---|---|---|
| `PartialPdfInfo` dataclass | `PartialPdfMergeInfo.cs` | blob_id/slide_index/x/y/extCX/extCY/rotation/z_index/content_control/h_align/v_align |
| `parse_partial_pdf_info()` | XML deserializer | 解析 `<partialPDF><info>` 块，UTF-16 BOM 支持，按 (slide_index, z_index) 排序 |
| `_set_fit_size()` | `PartialPdfMerger.SetFitSize()` | FitBoth / FitWidth / FitHeight / ScaleToFit，含 alignment 位移 |
| `_build_placement_matrix()` | `PdfPageRectangle.CreateFromPPTSize()` + `GetMatrix()` | PPTX Y-down → PDF Y-up 坐标翻转，affine = T·R·S·T_crop |
| `_source_rotation_matrix()` | `GetSourceRotationTransform()` | 90/180/270 旋转修正矩阵（仅当 CropBox 存在且 Rotate≠0 时应用） |
| `_compose()` | `ComposeTransform()` | outer · inner PDF affine 合成 |
| `apply_partial_pdf()` | `MergeAll()` + `ApppendPartialPdfOnSlide()` | 打开 main PDF、复制所有页、按 slide 叠加 XObject、支持 hidden_slide_indices |
| `apply_partial_pdf_from_xml()` | — | 一行调用：parse XML + apply |

### XML Schema（`<partialPDF>` 路径）

```xml
<WorkspaceMergeInfo>
  <PDFMerge />   <!-- 可同时存在；partial-merge CLI 只处理 partialPDF -->
  <partialPDF>
    <info blob-id="uuid" content-control="FitBoth"
          horizontal-alignment="Left" vertical-alignment="Top">
      <SlideIndex>1</SlideIndex>   <!-- 1-based -->
      <x>10.8</x>                  <!-- PPTX pts, upper-left, Y-down -->
      <y>92.95</y>
      <extCX>684</extCX>           <!-- bounding box width  (pts) -->
      <extCY>352.29</extCY>        <!-- bounding box height (pts) -->
      <rotation>0</rotation>       <!-- degrees CCW -->
      <z-index>0</z-index>
    </info>
  </partialPDF>
</WorkspaceMergeInfo>
```

### content-control 模式

| 值 | 行为 |
|---|---|
| `FitBoth` | 等比缩放塞入 bounding box，按 v/h-alignment 对齐 |
| `FitWidth` | 按宽度缩放，垂直居中 |
| `FitHeight` | 按高度缩放，水平居中 |
| `ScaleToFit` | 直接用 extCX×extCY（不保持比例） |

### 关键 Bug Fix（本 session 修复）

**`_page_display_size` — CropBox + Rotate 组合 scale 维度错误**

- **根本原因**：iText7 的 `CopyAsFormXObject` 会把 `/Rotate` bake 进 XObject，有效坐标空间已 swap；Python 的 `_page_as_form_xobject` 不 bake rotation，但 scale 基准必须一致。
- **修复**：当 CropBox 存在时，用 CropBox 尺寸经 Rotate swap 后的值做 `_set_fit_size` 的 src 维度：
  - Rotate ∈ {90, 270}：`display = (crop_h, crop_w)`
  - Rotate ∈ {0, 180}：`display = (crop_w, crop_h)`
- **验证**（`__temp_blob.xml`，CropBox=[20,30,800,630]，Rotate=90，extCX=514.83，extCY=386.12）：

  | | sx | sy |
  |---|---|---|
  | 修复前 | 0.787 ❌ | 0.463 ❌ |
  | 修复后 | **0.858** ✅ | **0.495** ✅ |
  | iText7 实测 | 0.858 | 0.495 |

---

## Benchmark 结果（本 session 新增）

工具：`C:/test/IText7Test/PdfMerge/bench_partial.py`  
比较方式：对 7 个测试 XML 各跑 3 次，用 `PdfCompareTmp.exe` 逐页对比。

```
XML                                        iText7   Python  Speedup  Match
------------------------------------------------------------------------
__temp_blob.xml         (PDFMerge+partial)  1596ms     573ms     2.8x   DIFF*
merge-info.rot90.xml    (PDFMerge+partial)  1197ms     490ms     2.4x   DIFF*
merge-info.xml          (PDFMerge+partial)  1178ms     499ms     2.4x   DIFF*
merge-info2 (2).xml     (partial only)      1125ms     483ms     2.3x     OK
merge-info2.xml         (partial only)      1107ms     511ms     2.2x     OK
merge-info3.xml         (PDFMerge only)      857ms     469ms     1.8x   DIFF*
WorkspaceMergeInfo.xml  (PDFMerge only)      855ms     471ms      N/A    ERR*
```

`*` 标注的 DIFF/ERR 均为**预期的 pipeline 差异**，不是 partial.py 的 bug：
- `DIFF*`（combined XML）：iText7 同时跑 `<PDFMerge>` + `<partialPDF>`；Python `partial-merge` 只处理 `<partialPDF>` 部分。纯 partial 页面的 diff 均 ≤ 2 字节（换行符差异）。
- `ERR*`：iText7 走了 whole-page-merge 路径，`<partialPDF>` 为空，Python 直接 copy。

**结论：Python 在 pure-partialPDF 场景与 iText7 结果完全一致，速度快 2.2–2.3x。**

---

## 已修复的关键 Bug（累计）

1. **`page_as_form_xobject`**：content stream 与 Resources 来自同一 `copy_foreign`，避免 foreign object 引用泄漏。
2. **Mode B MediaBox**：`_merge_fit_pdf_size` 显式写入外部页原生尺寸。
3. **`_compute_fit_main_matrix` CropBox**：直接用 CropBox 尺寸做 fit。
4. **`set_pdfua_metadata` foreign object**：用 `pikepdf.String(str(...))` 重构。
5. **多页主 PDF tag tree 翻倍**：`_copy_slide_kids` 加 `filter_src_page` 参数。
6. **`parse_page_range` 逆序拒绝**：`5-3` 抛出 ValueError。
7. **Windows 路径误解析为页码**：`parse_input_arg` 用 `rpartition(":")` + 纯数字检查。
8. **`_page_display_size` CropBox+Rotate scale 错误**：CropBox 尺寸须经 Rotate swap 后再用于 `_set_fit_size`（本 session 修复）。

---

## 重要设计决策

1. **pikepdf 而非 PyMuPDF**：PyMuPDF 无 StructTreeRoot 写入支持；pikepdf MPL-2.0 vs PyMuPDF AGPL。
2. **`add_pages_from(forms='preserve')`**：pikepdf ≥ 8.x 原生 API 替代 iText7 `PdfPageFormCopier`，自动处理 AcroForm 冲突重命名。
3. **`filter_src_page`**：多页源 PDF 的 StructTree 按 `/Pg` 过滤，避免每次 `merge_page_tags` 复制全部 Slide。
4. **PDF/UA `<Document>` 单例**：`_get_or_create_document` 保证 dst StructTreeRoot 下始终只有一个 `<Document>` 元素（PDF/UA-1 §7.1）。
5. **版本锁定 1.7**：`dst.save(output, min_version="1.7")`，对标 CTS2.0 `WriterProperties().SetPdfVersion(PdfVersion.PDF_1_7)`。
6. **partial.py 不 bake /Rotate**：`_page_as_form_xobject` 保留原始内容流（不 bake rotation），通过 `_source_rotation_matrix` + `_compose` 在 page-level cm 里修正，scale 基准用 CropBox+Rotate 的 display 尺寸确保与 iText7 一致。

---

## AcroForm 与 CTS2.0 的边界差异

- CTS2.0 的 Mode A/B 路径中，`CopyAnnotations`（Widget）调用已被注释掉。
- pdfmergepy **超出**了 CTS2.0 现有行为：外部 PDF 的 Widget 字段通过 `add_page_with_forms` 也被保留。
- partial.py 路径同样保留外部 AcroForm 字段（与 CTS2.0 partial 路径一致，CTS2.0 也未注释该调用）。
- 如需与 CTS2.0 composite 路径精确对齐（不保留外部表单），可在 `_merge_fit_*` 中改用 `dst.pages.append(pikepdf.Page(...))` 代替 `add_page_with_forms`。

---

## 未实现功能（下一步）

### Phase 3：后处理修复（中优先级）

| 功能 | 对标 CTS2.0 | 说明 |
|------|------------|------|
| `SanitizeDocumentK` | `PdfMergeTagHelper.SanitizeDocumentK` | 移除 iText7 错误写入的 StructTreeRoot/Page 对象；pikepdf 路径可能不产生此 bug |
| `FixInvalidTBodies` | `PdfMergeTagHelper.FixInvalidTBodies` | 修复 TBody 结构；用 `pdftagvalicate --validate` 验证后再决定 |
| `AlterChartAsFigure` | `PdfMerger.AlterChartAsFigure` | 把 `<Chart>` role 改为 `<Figure>`；低优先级 |
| Link Annot 复制 | `PdfMergeHelper.CopyAnnotations` | 超链接注解（含矩阵变换）；Mode A/B 路径里 CTS2.0 已注释掉 |

### Phase 4：性能与合规性基线（计划中）

- 用最大生产样本测 `merge_from_xml` + `apply_partial_pdf` 端到端耗时，与 iText7 比较。
- 用 `pdfmergepy info` JSON diff 对比 page geometry。
- 用 `pdftagvalicate --validate` 检查 PDF/UA 合规性。

### partial.py — combined XML 支持（待讨论）

当 XML 同时含 `<PDFMerge>` + `<partialPDF>` 时（如 `merge-info.xml`），CTS2.0 先跑 PDFMerge 再跑 partial overlay。  
当前 Python `partial-merge` CLI 只处理 `<partialPDF>` 部分。  
**选项**：在 `partial-merge` 命令里先调用 `merge_from_xml`（处理 PDFMerge），再调用 `apply_partial_pdf`（处理 partialPDF）。

---

## 已知遗留问题（低优先级）

- `prepend_content`：畸形 PDF（q/Q 不平衡）会破坏 graphics-state 栈（`composite.py` 注释标记）。
- `_detect_base_ctm`：仅扫 200 字节；有注释前缀的流会命中错误。
- `merge_page_tags`：不处理 Stream MCR（`/Stm` 引用）和 ObjRef。

---

## 文件结构

```
pdfmerge-py/
├── src/pdfmergepy/
│   ├── acroform.py      # AcroForm 字段合并（完成）
│   ├── cli.py           # CLI 入口（完成；含 partial-merge 子命令）
│   ├── composite.py     # Form XObject 叠合 Mode A/B（完成）
│   ├── merge.py         # Plain concat（完成）
│   ├── mergeinfo.py     # WorkspaceMergeInfo <PDFMerge> XML 解析（完成）
│   ├── partial.py       # Partial PDF sub-region overlay（完成；本 session 新增）
│   ├── pdfutil.py       # 页面范围/几何工具（完成）
│   └── tagtree.py       # StructTree plain-copy + XObject MCR 迁移（完成）
├── tests/
│   ├── test_acroform.py    # 11 用例
│   ├── test_merge.py       # 7 用例
│   ├── test_mergeinfo.py   # 17 用例
│   ├── test_partial.py     # 21 用例（本 session 新增）
│   ├── test_tagtree.py     # 16 用例
│   └── _proto_tagtree.py   # 原型脚本（不参与 pytest）
├── README.md
├── STATUS.md            # 本文件
└── pyproject.toml       # pikepdf>=9.0, pytest>=8.0, Python>=3.10
```

---

## 本 session Git 提交记录

| Commit | 内容 |
|--------|------|
| `4d62f40` | `feat: add partial PDF merge (sub-region XObject overlay)` — 新增 `partial.py`、21 个测试、CLI `partial-merge` 子命令 |
| `b651528` | `fix(partial): correct scale dims for CropBox+Rotate pages` — 修复 `_page_display_size` CropBox+Rotate 组合 scale 错误；新增 4 个回归测试 |

---

## 下一个 session 启动检查

```bash
cd C:/project_new/pdfmerge-py   # 或 clone 到本地

# 确认当前状态
python -m pytest tests/ --ignore=tests/_proto_tagtree.py -q
# 期望: 167 passed, 0 failed

# 运行 benchmark（需要 CTS2.0 LiveDocConverter 已编译）
cd C:/test/IText7Test/PdfMerge
python bench_partial.py

# 参考 C# 源码
# PartialPdfMerger.cs:  ApppendPartialPdfOnSlide, SetFitSize, GetTransformMatrix
# PdfPageRectangle.cs:  CreateFromPPTSize, GetMatrix
code "C:/project_new/content-transformation-service-v2/src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/PartialPdfMerger.cs"
```
