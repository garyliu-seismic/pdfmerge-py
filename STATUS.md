# pdfmergepy — Session Status Summary

> 供下一个 session 直接接手，无需重新阅读历史对话。

---

## 项目定位

`C:/project_new/pdfmerge-py` — CTS2.0 itext7 PDF merge 功能的 Python POC 重写。  
底层库：**pikepdf ≥ 9.0**（基于 QPDF，MPL-2.0）。  
对标源码：`C:/project_new/content-transformation-service-v2/src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/`

```
editable install : pip install -e .   （已安装）
测试命令         : python -m pytest tests/ --ignore=tests/_proto_tagtree.py -q
当前结果         : 51 passed, 0 failed  （0.63 s）
```

---

## 模块清单（全部已实现）

| 文件 | 行数 | 功能 | 对标 CTS2.0 |
|------|------|------|------------|
| `merge.py` | ~70 | plain concat：AcroForm + tag tree per-page | `CustomMerger.Merge` plain 路径 |
| `composite.py` | ~620 | Form XObject 矩阵叠合（Mode A / B） | `MergePDFToUseExternalPageSize` / `MergePDFToFitMainPdfSize` |
| `mergeinfo.py` | ~220 | WorkspaceMergeInfo XML 解析 + 路径解析 | `ExternalPDFMergeInfo` 序列化层 |
| `pdfutil.py` | ~75 | 页面范围解析 / page_geometry inspect | 工具层 |
| `acroform.py` | ~145 | AcroForm 字段复制 + 冲突重命名 + DR 合并 | `PdfPageFormCopier` / `CustomCopier` |
| `tagtree.py` | ~465 | StructTree plain-copy 合并（Phase 1） | `PdfMergeTagHelper.MergeRoleMap/WrapNewKidsUnderSect` |
| `cli.py` | ~130 | CLI（`merge` / `info` / `merge-xml`） | — |

---

## CLI 用法

```sh
# 合并整份 PDF
pdfmergepy merge a.pdf b.pdf -o out.pdf

# 合并指定页码（1-based，支持范围和逗号）
pdfmergepy merge a.pdf:1-3 b.pdf:2,4-5 -o out.pdf

# 检查页面几何（与 itext7 输出 diff 用）
pdfmergepy info out.pdf

# 按 WorkspaceMergeInfo XML 合并（CTS2.0 格式）
pdfmergepy merge-xml merge.xml --main main.pdf --inputs-dir blobs/ -o out.pdf
```

---

## 测试覆盖

| 测试文件 | 用例数 | 覆盖点 |
|---------|--------|-------|
| `test_merge.py` | 7 | 页面范围解析、plain concat、页面属性保留 |
| `test_mergeinfo.py` | 17 | XML 解析（direct/ApplySlide）、路径解析、矩阵数学、Mode A/B 集成、Form XObject resources |
| `test_acroform.py` | 11 | AcroForm 字段复制、名称冲突重命名、嵌套 parent/kids、/DR 合并、Mode A&B |
| `test_tagtree.py` | 16 | StructTree 结构、RoleMap、ParentTree slot、/Pg 引用、PDF/UA metadata、mixed tagged/plain |

`tests/_proto_tagtree.py`：原型脚本，已排除在 pytest 之外。

---

## 已修复的关键 Bug

1. **`page_as_form_xobject`**：content stream 与 Resources 来自同一 `copy_foreign` 调用，避免 foreign object 引用泄漏（Fix #1）。
2. **Mode B MediaBox**：`_merge_fit_pdf_size` 显式写入外部页原生尺寸，不依赖 pikepdf append 的隐式行为（Fix #2）。
3. **`_compute_fit_main_matrix` CropBox**：直接用 CropBox 尺寸做 fit，去掉错误的 MediaBox 二次修正（Fix #3）。
4. **`set_pdfua_metadata` foreign object**：用 `pikepdf.String(str(...))` 而非直接赋值 src 的字符串对象。
5. **多页主 PDF tag tree 翻倍**：`_copy_slide_kids` 加 `filter_src_page` 参数，按 `/Pg` 过滤只复制当前页对应的 Slide。
6. **`parse_page_range` 逆序拒绝**：`5-3` 现在抛出 `ValueError("invalid page range '5-3'")`。
7. **Windows 路径不被误解析为页码**：`parse_input_arg` 用 `rpartition(":")` + 纯数字检查，`C:/tmp/file.pdf` 不会把 `tmp/file.pdf` 当范围。

---

## 重要设计决策

1. **pikepdf 而非 PyMuPDF**：PyMuPDF 无 StructTreeRoot 写入支持；pikepdf MPL-2.0 vs PyMuPDF AGPL。
2. **`add_pages_from(forms='preserve')`**：pikepdf ≥ 8.x 原生 API 替代 iText7 `PdfPageFormCopier`，自动处理 AcroForm 冲突重命名，返回 `PageCopyResult(fields_added, renamed_fields, ...)`。
3. **`filter_src_page`**：多页源 PDF 的 StructTree 按 `/Pg` 过滤，避免每次 `merge_page_tags` 复制全部 Slide。
4. **`<Document>` 节点无 `/Pg`**：PDF 规范 §14.7.2 明确 document-level 容器无需 /Pg；测试辅助函数 `_all_pg_present` 已跳过 `_DOC_LEVEL_ROLES = {/Document, /Part}`。
5. **PDF/UA `<Document>` 单例**：`_get_or_create_document` 保证 dst StructTreeRoot 下始终只有一个 `<Document>` 元素（PDF/UA-1 §7.1）。
6. **版本锁定 1.7**：`dst.save(output, min_version="1.7")`，对标 CTS2.0 `PdfMerger.cs` `WriterProperties().SetPdfVersion(PdfVersion.PDF_1_7)`。

---

## AcroForm 与 CTS2.0 的边界差异

- CTS2.0 的 Mode A/B 路径中，`CopyAnnotations`（Widget）调用在 C# 源码里**已被注释掉**。
- pdfmergepy **超出**了 CTS2.0 现有行为：外部 PDF 的 Widget 字段通过 `add_page_with_forms` 也被保留。
- 如需与 CTS2.0 精确对齐，可在 `_merge_fit_pdf_size` / `_merge_fit_main_size` 中改用 `dst.pages.append(pikepdf.Page(...))` 代替 `add_page_with_forms`。

---

## 未实现功能（下一步）

### Phase 2：Form XObject 路径的 tag tree 迁移（最高优先级）

对标：`PdfMergeTagHelper.MigrateTagsForXObject`（CTS2.0，约 700 行 C#）

**触发场景**：Mode A / Mode B 合成时，主 PDF 页面变成 Form XObject 叠在外部页上，
iText7 不会自动迁移主页 tag tree，CTS2.0 用 `MigrateTagsForXObject` 手工完成。

**需实现的核心步骤**：

```
Step 1  从 src StructTree 查找该页的 MCR（PageMarkedContentReferences）
        → 遍历 /ParentTree 数字树反查（按 /StructParents 定位 slot）

Step 2  扫描 src content stream，提取所有 /MCID token
        → 正则扫 page_obj.read_bytes()（已有 _detect_base_ctm 先例）

Step 3  为 XObject 写 ParentTable 到 dst /ParentTree
        → slot = pageCount + xObjectIndex
        → 写 /StructParents 到 XObject stream_dict

Step 4  在 dst StructElem /K 里写 MCR dict（含 /Stm → XObject ref）
        → {/Type /MCR, /Pg dst_page, /Stm xobj_ref, /MCID n}
```

**关键函数映射**：

| CTS2.0 C# 函数 | Python 待实现（tagtree.py） | 难度 |
|---------------|---------------------------|------|
| `MigrateTagsForXObject` | `migrate_tags_for_xobject(src_page, xobj, dst_page, dst)` | 极高 |
| `EnsureAncestorChain` | `_ensure_ancestor_chain(src_node, parent_map, attach_point, dst)` | 高 |
| `WriteXObjectParentTable` | `_write_xobject_parent_table(dst_root, xobj, mcid_to_parent, max_mcid)` | 高 |
| `ExtractMcidsFromPageContent` | `_extract_mcids_from_content(page_obj)` | 中 |
| `EnsureUniqueXObjectStructParents` | `_ensure_unique_xobj_struct_parents(xobj_stream)` | 低 |
| `ResolveDestAttachPoint` | `_resolve_dest_attach_point(dst_root, dst_page)` | 中 |

**接入点**（composite.py，当前用 plain `add_page_with_forms`）：
- `_merge_fit_pdf_size`：`append_content` 之后，`add_xobject_to_resources` 之前
- `_merge_fit_main_size`：`page_as_form_xobject` 之后

**参考 C# 行号**（`PdfMergeTagHelper.cs`）：
- `MigrateTagsForXObject`：line 886
- `EnsureAncestorChain`：line 1117
- `WriteXObjectParentTable`：line 1216
- `ExtractMcidsFromPageContent`：line 1055

### Phase 3：后处理修复（中优先级）

| 功能 | 对标 CTS2.0 | 说明 |
|------|------------|------|
| `SanitizeDocumentK` | `PdfMergeTagHelper.SanitizeDocumentK` | 移除 iText7 错误写入 `<Document>.K` 的 StructTreeRoot/Page 对象；pikepdf 路径不产生此 bug，**可能不需要** |
| `FixInvalidTBodies` | `PdfMergeTagHelper.FixInvalidTBodies` | 修复 TBody 结构问题；用 `pdftagvalicate --validate` 验证后再决定 |
| `AlterChartAsFigure` | `PdfMerger.AlterChartAsFigure` | 把 `<Chart>` role 改为 `<Figure>`；低优先级 |
| Link Annot 复制 | `PdfMergeHelper.CopyAnnotations` | 超链接注解（含矩阵变换）；Mode A/B 路径里 CTS2.0 已注释掉 |

### Phase 4：性能基线（计划中）

- 用最大生产样本测 `merge_from_xml` 耗时，与 iText7 比较
- 用 `pdfmergepy info` JSON diff 对比 page geometry
- 用 `pdftagvalicate --validate` 检查 PDF/UA 合规性

---

## 已知遗留问题（低优先级，已注释标记）

- `prepend_content`：畸形 PDF（q/Q 不平衡）会破坏 graphics-state 栈（`composite.py` 第 ~200 行注释）
- `_detect_base_ctm`：仅扫 200 字节；有注释前缀的流会命中错误
- `merge_page_tags`：不处理 Stream MCR（`/Stm` 引用）和 ObjRef——Phase 2 的工作

---

## 文件结构

```
C:/project_new/pdfmerge-py/
├── src/pdfmergepy/
│   ├── acroform.py      # AcroForm 字段合并（完成）
│   ├── cli.py           # CLI 入口（完成）
│   ├── composite.py     # Form XObject 叠合 Mode A/B（完成；Phase 2 接入点预留）
│   ├── merge.py         # Plain concat（完成）
│   ├── mergeinfo.py     # WorkspaceMergeInfo XML 解析（完成）
│   ├── pdfutil.py       # 页面范围/几何工具（完成）
│   └── tagtree.py       # StructTree plain-copy Phase 1（完成；Phase 2 待实现）
├── tests/
│   ├── test_acroform.py    # 11 用例
│   ├── test_merge.py       # 7 用例
│   ├── test_mergeinfo.py   # 17 用例
│   ├── test_tagtree.py     # 16 用例
│   └── _proto_tagtree.py   # 原型脚本（不参与 pytest）
├── README.md            # 安装/用法（注意："Out of scope" 描述已过时）
├── STATUS.md            # 本文件
└── pyproject.toml       # pikepdf>=9.0, pytest>=8.0, Python>=3.10
```

---

## 下一个 session 启动检查

```bash
cd C:/project_new/pdfmerge-py

# 确认当前状态
python -m pytest tests/ --ignore=tests/_proto_tagtree.py -q
# 期望: 51 passed, 0 failed

# 查看 Phase 2 接入点
grep -n "MigrateTagsForXObject\|Phase 2\|TODO\|FIXME" \
  src/pdfmergepy/composite.py src/pdfmergepy/tagtree.py

# 参考 C# 源码
code "C:/project_new/content-transformation-service-v2/src/CTS/\
Seismic.CTS.Implements.LiveDocs/IText7/PdfMergeTagHelper.cs"
```
