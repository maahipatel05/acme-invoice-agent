// Dependency-free macOS fallback; portable deployments should install PyMuPDF.
import Foundation
import PDFKit
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let doc = PDFDocument(data: data), !doc.isEncrypted, doc.pageCount <= 30 else {
    fputs("Unreadable, encrypted, or oversized PDF\n", stderr)
    exit(2)
}
print(doc.string ?? "")
