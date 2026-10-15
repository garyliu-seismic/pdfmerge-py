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
当前结果         : 326 passed, 0 failed   (含 real-data regression，约 4 分钟；conftest.py 自动把 src/ 加入 sys.path/PYTHONPATH)
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
| `cli.py` | ~330 | CLI（`merge` / `info` / `merge-xml` / `partial-merge` / `split` / `select` / `delete` / `append` / `page-count` / `list-fonts` / `embed-fonts` / `remove-fonts`） | — |
| `pageranges.py` | ~110 | **页码表达式 `1,3,6-8,10-`**（精确移植） | `PdfUtility.EnumAllPageSections` / `PageSection.IsInclude` |
| `prune.py` | ~480 | **删页 + 修剪 StructTree/ParentTree/AcroForm/Outlines/Dests/PageLabels** | `CopyPagesTo` + `PdfPageFormCopier` 的效果 |
| `pageops.py` | ~170 | **select / delete / split / append / page-count** | `PdfSelector` / `PdfSplitter` / `PdfSplitConverter` / `PdfManager.GetPageCount` |
| `fonts.py` | ~350 | 字体发现（继承 Resources、Form XObject、Pattern、SMask、Annot AP、Type3、AcroForm DR）、`list_fonts`、`remove_fonts` | `InternalUtility.GetAllFontRelatedObjects` / `RemoveFonts` / `GetUsedFonts` |
| `fontmap.py` | ~330 | font-map.json 加载 + PostScript/Family+Style 匹配 | `FontMapItem` / `FontMapUtility` / `FontDictionary` / `PdfOtfFontProcessor.__GetRelation` |
| `fontembed.py` | ~480 | 就地嵌入完整字体（simple + Type0），CID/GID 校验 | `PdfOtfFontProcessor.EmbedFont` |

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
| `test_fonts.py` | **36** | 字体发现/匹配/嵌入/移除/CLI/加密/渲染（pypdfium2） |
| `test_regression_fonts.py` | **20** | 真实样本：18 个 blob PDF（嵌入 + pdftagvalicate 31-001）、CloudFonts baseline、真实 CFF OTF |
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

## split / select / delete / append / page-count（feature/split-select-append）

### 策略（delete-from-copy，而非 add_pages_from）

iText7 `CopyPagesTo` 会把 tag tree + ParentTree + AcroForm 一起复制；pikepdf `add_pages_from` 不带 tag（会悬空）。
因此 select/delete/split 采用：**打开源文件整份保留 → 删掉不要的页 → 修剪所有仍指向被删页的结构 → 保存**（`prune.keep_only_pages`）。
`append` 就是 `merge.merge_files`（已经是 generic append：tagtree.merge_page_tags + acroform），CLI `append` 直接复用，不重复实现。

`prune.py` 修剪清单：
- StructTreeRoot：删除落在被删页上的 MCID/MCR/OBJR kid；kid 清空的 StructElem 级联删除（自底向上、迭代实现，无递归深度问题）；幸存元素若 `/Pg` 指向被删页则去掉 `/Pg`；过滤 `/Ref`、`/IDTree`。
- ParentTree：全部元素已删的条目整条丢弃；数组里已删元素置 null（下标 = MCID，不压缩）；重写为扁平 `/Nums`（避免 `/Limits` 过期）。
- AcroForm：只保留在保留页上有 widget 的字段（共享字段只保留保留页上的 widget kid）；无 widget 的字段保留；删除 `/XFA`；无字段则删掉 `/AcroForm`；过滤 `/CO`。
- Outlines：目标页被删的条目移除（有存活子孙时保留容器、去掉其 dest），重连 Prev/Next/First/Last/Parent，重算 `/Count`（保留 open/closed 符号）。
- 命名目的地（`/Names/Dests`、`/Dests`）、保留页上 Link 注解的 `/Dest` 与 GoTo `/A`、`/OpenAction`；`/PageLabels` 重新编号使保留页**显示的页标不变**。
- 安全网：最后扫描所有对象，把仍指向已删页的引用置空，保证不会把孤儿页（带全部内容）写进输出。

输出：PDF 1.7（`force_version`）；保留 /Lang、/Title、整份 docinfo；`ViewerPreferences/DisplayDocTitle=true`；有 tag 的源另写 `/MarkInfo/Marked` 与 XMP `pdfuaid:part=1`；`remove_unreferenced_resources()`；Producer = pdfmergepy。

### 与 C# 的语义对应 / 已知差异

| 项 | C# | Python | 说明 |
|---|---|---|---|
| 页码表达式 | `EnumAllPageSections` | `pageranges.py` 逐条移植 | 单页 End=0；`4-`→MaxValue；`-6`→MinValue；倒序 `7-3` 匹配空集（不报错）；越界/重复无害；`0`、`-`、非数字、>int32 报错；输出保持原页序、不重复 |
| split | `0000.pdf`…，余页放最后一个文件，页用完即停 | 同 | `parse_split_counts` 同 `__ParsePage`（必须 >0 整数）；0 页文档无输出文件 |
| 空结果 | iText 写 0 页文件会抛异常 | 明确报 ValueError（"empty document"） | select/delete 结果为空时 |
| 未打 tag 的源 | `SetTagged()` 会生成空 struct tree | **保持未打 tag**（不写空 StructTreeRoot、不写 pdfuaid） | 故意偏离：对无 tag 内容声称 PDF/UA 不实 |
| 非 Title 的 docinfo（Author 等） | 新文档只复制 Title/Lang | 全部保留 | 超集 |
| 页标（PageLabels） | iText 版本相关 | 重新编号保持显示不变 | |
| 无 widget 的 AcroForm 字段 | 不复制 | 保留 | 可能带计算脚本，无害 |
| 加密 | 抛异常 | owner-only 加密可直接处理并输出**未加密**文件；需要 user password 时抛 `PdfInputError`（可 `--password`） | |
| 损坏文件 | 抛异常 | `PdfInputError: not a valid / readable PDF`，CLI 退出码 1 | |
| split 多块 | 每块重开源文件 | 同，每块重新 prune（O(块数 × 树大小)） | 130 页带 tag 文档 split 约 1.2s |

### 设计决策 / fork
1. **删页后保留整份文档而非复制页**：唯一能保证 tag tree 完整（含 MCR `/Stm`、OBJR、跨页元素）的方案。代价：每块 split 都要走一次 prune。
2. **跨页 StructElem 的部分删除**：Table/List/Sect 这类跨页容器只丢被删页上的 kid，容器本身保留（语义上可能出现 TBody 失去表头行，与 iText CopyPagesTo 行为同类，未单独处理）。
3. `09-007`（首个标题必须是 H1）取决于**保留了哪些页**（删掉含 H1 的页就会 fail），回归测试把它列为 content-dependent 允许项。
4. 顺手修复 merge 引擎的既有缺陷：源未打 tag 或页面无 marked content 时，目标页遗留/分配了悬空 `/StructParents`（ParentTree 里没有该 key）。`merge.merge_files` 现在在 `merge_page_tags` 返回 False 时删除该 key（有测试）。

### 验证（见测试）
- `tests/test_pageranges.py` 22；`tests/test_pageops.py` 40（split 命名/余页、select/delete 页数与内容、struct tree 完整性、AcroForm、outline/dests/labels、加密/损坏输入、CLI）；
- `tests/test_regression_pageops.py` 41：每个真实样本跑 select/delete/split/append，`pdftagvalicate` 的 Fail/Error id 必须是源文件自身基线的子集（`09-007` 例外），且 `tests/tagcheck.py` 的结构完整性问题数不得多于源、孤儿页对象恒为 0。
- 性能（本机，Python 3.12，pikepdf 10.13）：4.6MB/49p 无 tag 文件 select 1-5 ≈ 0.02s、峰值 WS 35MB；4.1MB/130p 带 tag（4477 个 StructElem 被剪）select 1-5 ≈ 0.55s、split 5 页一块（2 个文件）≈ 1.2s、峰值 WS ≈ 65MB（均不含 0.15s 解释器 import）。

### 未覆盖 / 已知缺口
- 未与 iText7 逐字节对比（无 iText 基线，仅与源文件自身的验证基线比较）。
- Article threads、`/AA`、RichMedia/3D 等罕见结构只由安全网置空引用，未做语义修剪。
- 页面范围里的重复/乱序不会产生重复页或重排（与 C# predicate 语义一致）；若以后需要“按表达式顺序重排”需另行设计。
- StructTree 里 `/Pg` 缺省且 int MCID 的元素（页由祖先隐含）按“保留”处理。

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
│   ├── tagtree.py       # StructTree plain-copy + XObject MCR 迁移（完成）
│   ├── pageranges.py    # 页码表达式（新）
│   ├── pageops.py       # select/delete/split/append/page-count（新）
│   └── prune.py         # 删页后的 tag/form/outline 修剪（新）
├── tests/
│   ├── conftest.py            # src/ → sys.path + PYTHONPATH（子进程也可用）
│   ├── test_pageranges.py     # 22 用例（新）
│   ├── test_pageops.py        # 40 用例（新）
│   ├── test_regression_pageops.py  # 41 用例（真实样本 + pdftagvalicate，新）
│   ├── pdfbuilder.py / tagcheck.py # 合成 PDF 构造器 / struct 完整性检查器（新）
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

## Regression Test 框架（下一 session 新增）

### 工具

| 文件 | 位置 | 功能 |
|------|------|------|
| `bench_regression.py` | `C:/test/IText7Test/PdfMerge/` | 全量 regression：每个 XML 跑 iText7 + Python，PdfCompareTmp 逐页对比 |
| `PdfCompareTmp` (已扩展) | `C:/test/IText7Test/PdfMerge/PdfCompareTmp/` | 新增 `geometry`、`matrix` 子命令，可直接调用 iText7 API 验证矩阵 |

### 调用方式

```bash
cd C:/test/IText7Test/PdfMerge

# 全量跑所有 41 个 XML
python bench_regression.py --runs 1

# 只跑某个 XML
python bench_regression.py --xml "__blob_synthetic-rot90-crop.xml"

# iText7 CLI（CWD = C:/test/IText7Test/PdfMerge，InputFiles 为相对路径）
Seismic.CTS.Converter.LiveDoc.exe convert /Method:livedoc-merge \
  <main.pdf> pdf --method-merge \
  /InputFiles:inputs /WorkspaceMergeInfo:@<xml> /ITextVersion:7 /Output:<out.pdf>

# Python pipeline（两步 chain for combined XML）
pdfmergepy merge-xml <xml> --main <main.pdf> --inputs-dir inputs -o step1.pdf
pdfmergepy partial-merge <xml> --main step1.pdf --inputs-dir inputs -o out.pdf
```

### 结果（41 个 PDFMerge XMLs，main=s1.t1.t2_43.pdf）

```
34 × MATCH(fmt)  — dcontent≤50 bytes，纯格式差异
 7 × DIFF(text)  — iText7 CRLF bug（见下），Python 正确
```

### MATCH(fmt) 详解（Python 与 iText7 逻辑完全一致，仅格式差异）

| 差异项 | iText7 | Python |
|--------|--------|--------|
| 浮点精度 | `0.9174312`（7位）| `0.917431`（6位）|
| 零分量 | `0 -0.733813` | `0.000000 -0.733813` |
| XObject 名 | `/Fm1` | `/Xobj0` |
| 多余包裹 | `q\nq\n/Fm1 Do\nQ\nQ\n\nQ\n` | `q\n/Xobj0 Do\nQ\nQ\n` |

### DIFF(text) 详解（iText7 的 CRLF bug，Python 行为正确）

**根本原因**：`GetPageBaseCTM` 用 `\nq\n` 检测 `q` guard，但 Windows PDF 的 CRLF `q\r\n` 不匹配，导致 guard check 失败，误检测内部的 `cm`。

**受影响 blob**（ext page content stream 含 CRLF `q\r\n` + 内部 `cm`）：
- `08cccc1b`、`19fe2e90`、`866bbbca`、`e6a73263`、`e8ae0dbc`、`fae3c6f6`、`Fiat500`

**行为对比**（以 `19fe2e90`, ext=288×72，main=654×834 为例）：

| | iText7（有 bug）| Python（正确）|
|---|---|---|
| 检测到 base CTM | `[288, 0, 0, 72, 0, 0.000008]` ❌ 误检 | 未检测到 ✅ |
| effectiveMatrix | `[0.00030, 0, 0, 0.00120, 0, 0]`（极小！）| `[0.08631, 0, 0, 0.08631, 0, 0]`（正确）|
| 视觉大小 | 0.23×0.72 pts（几乎不可见）❌ | 72×56 pts ✅ |
| text 提取 | 0 chars ❌ | 2995 chars ✅ |

Python 不需要修改；`bench_regression.py` 的 verdict 逻辑用 `DIFF(text)` 标记这 7 个 case。

### 关键发现：旧 DLL 问题

`Seismic.CTS.Implements.LiveDocs.dll` mtime = **2026-07-22**，已包含最新 source（最后 git commit = 2026-06-02）。
之前 session 用的旧文件（`bench_out/` 目录）含 matrix `0.93525`，来自更早的 DLL 版本。**以 `regression_out/` 为准**。

---

## 下一个 session 启动检查

```bash
cd C:/project_new/pdfmerge-py   # 或 clone 到本地

# 确认当前状态
python -m pytest tests/ --ignore=tests/_proto_tagtree.py -q
# 期望: 326 passed, 0 failed

# 运行 benchmark（需要 CTS2.0 LiveDocConverter 已编译）
cd C:/test/IText7Test/PdfMerge
python bench_partial.py

# 参考 C# 源码
# PartialPdfMerger.cs:  ApppendPartialPdfOnSlide, SetFitSize, GetTransformMatrix
# PdfPageRectangle.cs:  CreateFromPPTSize, GetMatrix
code "C:/project_new/content-transformation-service-v2/src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/PartialPdfMerger.cs"
```


---

## 字体：embed-fonts / remove-fonts / list-fonts（branch `feature/embed-fonts`）

仅用 pikepdf（MPL-2.0）+ fontTools（MIT）；无 PyMuPDF/AGPL。

```sh
pdfmergepy list-fonts in.pdf [--missing-only] [--password P]      # JSON: name,type,embedded,subset,font_file,pages,used_in
pdfmergepy embed-fonts in.pdf -o out.pdf --font-map font-map.json --font-dir DIR --font Helvetica=arial.ttf \
          [--strict] [--ignore-license] [--allow-unverified-cid] [--remap-cid-via-tounicode] [--report r.json|-] [--require-all]
pdfmergepy remove-fonts in.pdf -o out.pdf [--min-size BYTES] [--subset yes|no] [--name-regex RE] [--large-pdf-rule]
```

### 字体来源（对标 FontMapItem/FontMapUtility）
- `--font-map X`：C# `font-map.json`（`{"files":[{path,names[],styles,format,...}]}`，path 相对 JSON 目录）或 C# 文本形式 `Name|path;Name|path`。
- `--font-dir D`：有 `D/font-map.json` 就用它（同 C#），否则递归扫描 `*.ttf/otf/ttc/pfb`（TTC 逐 face 展开）。
- `--font NAME=path`：直接指定，NAME 作为精确别名（PostScript/family）。
- 名称/样式实际从字体文件读取（fontTools）；JSON `names` 仅追加别名。相同元数据的重复字体只保留第一个（同 C#）。

### 匹配（对标 `__GetRelation`）
BaseFont 去掉 `ABCDEF+` 前缀 -> 1) PostScript/FullName/别名精确匹配（忽略大小写/空格/连字符）-> 2) Family+Style（移植 C# Regular/Bold/Italic/BoldItalic 各分支，含 "Use Regular instead" 回退）。
扩展：除 `Family,Style` 外还识别 `Family-Style`、`FamilyBoldMT`、`ArialMT`。`--strict` 关闭 Bold->Regular 回退。回退匹配会在 warnings 中标出。

### 设计决定
1. **就地修改**字体字典（C# 新建字体并改引用，旧对象成孤儿）：所有引用（页面/Form/Annot/DR）保持有效，无孤儿对象，同一字体文件流在多个字体间共享。
2. **保留** 原 /Encoding、/Widths、/ToUnicode、/W、/DW、/Flags；缺失时才补 /Widths（由字体 hmtx + 编码推出）、FontDescriptor 的 FontBBox/Ascent/Descent/CapHeight/ItalicAngle/StemV/Flags。C# 是用 WINANSI 重建 simple font，只适用于单字节 WinAnsi 文本；本实现对 Type0/自定义编码同样有效。
3. **不改内容流、不改结构树/tags**（测试断言 StructTreeRoot 与 Contents 字节不变）。BaseFont/FontName 改为字体文件的 PostScript 名（去掉 subset 前缀，因为嵌入的是完整字体）。
4. **字体程序存储（OpenType vs Type1C 的选择）**：glyf -> `/FontFile2`（+Length1）；CFF OTF -> 取出裸 `CFF ` 表存 `/FontFile3 /Type1C`（simple，Subtype 改 /Type1）或 `/CIDFontType0C`（Type0）；**不使用 /OpenType 包装**（兼容性最好，不要求 PDF 1.6）。CID-keyed CFF 不能用于 simple font -> 跳过。`.pfb` -> `/FontFile`（Length1/2/3），仅 simple。
5. **Type0 的 CID/GID 安全**：只处理 Identity-H/V（CIDSystemInfo Ordering=Identity）。用 /W 宽度 vs hmtx、/ToUnicode vs cmap 校验 "CID == 所供字体的 GID"（>=90% 一致才通过）；不一致 -> **跳过并 warning**（不静默损坏）；没有任何证据（无 /W 无 ToUnicode）-> 跳过，除非 `--allow-unverified-cid`。`--remap-cid-via-tounicode`：不一致时用 ToUnicode 重建 /CIDToGIDMap（仅 glyf；重建后再用 /W 复核，复核不过仍跳过）。预定义 CMap（90ms-RKSJ-H 等，CID 为 Adobe collection CID）一律跳过。
6. **许可**：OS/2 fsType bit1（Restricted License）默认不嵌入（C# 的 `__CannotEmbedded` 实际恒为 false，即不检查）；`--ignore-license` 可覆盖。
7. **加密**：未给 `--password` -> 明确报错 "PDF is encrypted; supply the password with --password"（rc=1）；密码错误同理。输出为**解密后**的 PDF（未再加密）。
8. **remove-fonts** 对标 `RemoveFonts` + `PdfRemoveFontsConverter` 谓词：删除 FontFile/2/3 条目（simple + 所有 DescendantFonts），`--min-size` = 仅删 **大于** N 字节者，`--subset yes|no`；`--large-pdf-rule` = OfficeUtility 的 "PDF >=10MB 才处理、字体 >=5MB 才删"。`only-system-fonts` 未移植（无 Utility.IsSystemFont），用 `--name-regex` 代替。
9. **发现范围优于 C#**：C# `CollectFonts` 在 InternalUtility.cs:123 读错字典（用 page dict 而非 XObject dict），实际不递归 Form XObject，也忽略继承 /Resources；本实现递归 Form/Pattern/SMask/Annot AP(/N /D /R)/Type3 /Resources/AcroForm DR，沿 /Parent 取继承 Resources，按 objgen 记忆化 + 环保护（深度 50）。

### 验证结果（证据）
- 真实样本 `C:/test/IText7Test/PdfMerge/inputs`（18 个 PDF 含非嵌入字体：Helvetica/Times-Roman/Arial/Arial,Bold/Courier-Bold/ZapfDingbats/MinionPro-Regular）：用 Windows 系统字体（仅测试用；Wingdings/Garamond 只是 ZapfDingbats/Minion 的机制性替身）**非嵌入字体 49 -> 0**；pdftagvalicate 31-001 **8 个 Fail->Pass，0 个仍 Fail，其余 Pass 不变，无新 Fail/Error**；pdfium 前 3 页文本抽取前后一致。最大样本 4.6MB/49 页：0.66s、峰值 RSS 46MB（含解释器）。
- CTS2.0 `LocalTest/CloudFonts/baseline.pdf`（6 个 subset 字体）：remove-fonts -> embed-fonts（CloudFonts 目录扫描 2217 文件约 4s）：4 个 simple 字体嵌入，渲染与 baseline 像素差 0.04%（去字体后 1.6%）；2 个 Type0 subset 字体因 GID 顺序与 CloudFonts 文件不同被**正确跳过**（/W 仅 18-23% 吻合）。`--remap-cid-via-tounicode` 在此仍拒绝（该 PDF 的 ToUnicode 只覆盖 8-18 个 CID，/W 复核失败）。
- 真实 CFF OTF（CTS CustomFonts/AllspringSanomat）：simple -> Type1C、Type0 -> CIDFontType0C，pdfium 渲染出真实字形。

### 未验证 / 已知缺口
- **未验证**：pdfium 以外的查看器（Acrobat/Foxit）渲染；与 C# 在 CloudFonts 集成用例（0001.pptx -> PDF -> EmbedFont）上的对等（需要 Office/Foxit 环境，未运行）；PAC 对嵌入结果的判定（只验证了 pdftagvalicate 31-001）。
- 缺口 vs C#：不做子集化（与 C# 相同，嵌入完整字体，输出明显变大，如 1KB -> 480KB）；去重只按 "相同 PS/Family/Style/类型"；无 `only-system-fonts`；Type3 不处理（本来内嵌）；.pfb 仅验证流结构（Length1/2/3），未做渲染验证；CFF2/可变字体不支持；Type0 + 预定义 CMap（CJK）不支持；多个候选字体并存时取目录顺序第一个（C# 同为 FirstOrDefault）；Bold->Regular 回退与 C# 一样不会合成粗体。
