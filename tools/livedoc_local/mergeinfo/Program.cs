using DocumentFormat.OpenXml.Packaging;
using Seismic.CTS.Implements.LiveDocs;
using Seismic.CTS.Implements.LiveDocs.Domain;
using P = DocumentFormat.OpenXml.Presentation;

// Office-free port of PowerPointConverterBase.__ProcessLiveDoc (content-transformation-service-v2,
// PowerPointConverterBase.cs ~L599-680). OpenXml only, no Office / CTS host.
//   removes the modification verifier, strips local-file image relationships,
//   builds SlideInfo (Hidden = slide.Show == false), runs LiveDoc.BeforeConversion
//   (strips placeholder slides), writes merge-info.xml.
// usage: MergeInfoTool <in.pptx> <outDir> <inputsDir> [<inputsDir2> ...]
//   -> <outDir>/stripped.pptx, <outDir>/merge-info.xml, and one summary line on stdout:
//   SUMMARY slides=<n> hidden=<n> expected_pages=<n> merge_info=<0|1> changed=<0|1>
if (args.Length < 3)
{
    Console.Error.WriteLine("usage: MergeInfoTool <in.pptx> <outDir> <inputsDir>");
    return 2;
}
string src = args[0], outDir = args[1];
string[] inputDirs = args.Skip(2).ToArray();   // one or more dirs holding {blobId}.pdf
Directory.CreateDirectory(outDir);
string newFile = Path.Combine(outDir, "stripped.pptx");
File.Copy(src, newFile, true);
bool hasInfo = false, anyChange = false;
var list = new SlideInfoList();
using (var presDoc = PresentationDocument.Open(newFile, true, new OpenSettings { AutoSave = false }))
{
    bool changed = false;
    var pres = presDoc.PresentationPart!.Presentation;
    if (pres.ModificationVerifier != null) { pres.ModificationVerifier.Remove(); changed = true; }
    string xml = OpenXmlExtensions.GetCustomXmlByRootName(presDoc.PresentationPart, pres, LiveDocConstants.WorkspaceMergeInfo);
    foreach (var slide in presDoc.PresentationPart.SlideParts)
    {
        var ext = slide.ExternalRelationships.Where(er =>
            er.RelationshipType == "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
            && er.Uri.IsAbsoluteUri && er.Uri.IsFile).ToArray();
        foreach (var e in ext) { slide.DeleteExternalRelationship(e); changed = true; }
    }
    int idx = 0;
    foreach (P.SlideId sid in pres.SlideIdList!.Elements<P.SlideId>())
    {
        idx++;
        if (sid.RelationshipId?.Value is not string rId || presDoc.PresentationPart.GetPartById(rId) is not SlidePart sp) continue;
        list.Add(new SlideInfo { Hidden = sp.Slide?.Show?.Value == false, SlideIndex = idx, LocalId = (int)(sid.Id?.Value ?? 0) });
    }
    if (!string.IsNullOrWhiteSpace(xml))
    {
        hasInfo = true;
        var info = WorkspaceMergeInfo.DeserializeFromXml(xml);
        info.SlideInfo = list;
        xml = info.ToXml();
        var live = new LiveDoc(new LiveDocContext(), xml, LiveDocFileInputHandles.FromFunc(t =>
        {
            string loc = t.Item1;
            foreach (var d in inputDirs)
            {
                string q = Path.Combine(d, loc + ".pdf");
                if (File.Exists(q)) return q;
            }
            string p = Path.Combine(inputDirs[0], loc + ".pdf");
            return File.Exists(loc) ? loc : p;
        }, "pdf"));
        live.BeforeConversion(presDoc, out bool c2);
        changed |= c2; xml = live.MergeXml;
        File.WriteAllText(Path.Combine(outDir, "merge-info.xml"), xml);
    }
    else Console.Error.WriteLine("WARNING: no WorkspaceMergeInfo custom xml found (no merge-info.xml written)");
    if (changed) { presDoc.Save(); }
    anyChange = changed;
}
// Re-read counts: BeforeConversion may have removed placeholder slides.
using (var d2 = PresentationDocument.Open(newFile, false, new OpenSettings()))
{
    var ids = d2.PresentationPart!.Presentation.SlideIdList?.Elements<P.SlideId>().ToList() ?? new List<P.SlideId>();
    int hidden = 0;
    foreach (var sid in ids)
        if (sid.RelationshipId?.Value is string r && d2.PresentationPart.GetPartById(r) is SlidePart sp && sp.Slide?.Show?.Value == false) hidden++;
    Console.WriteLine($"SUMMARY slides={ids.Count} hidden={hidden} expected_pages={ids.Count - hidden} merge_info={(hasInfo ? 1 : 0)} changed={(anyChange ? 1 : 0)}");
}
return 0;
