import Foundation
import Testing

/// The capture flow works on any object, so its words must never name a kind of part (invariant: no product or
/// part vocabulary in code). Close-up guidance comes from the object itself (far-only surfaces, coverage gaps, the
/// person's taps) or from the backend's generic vocabulary, never from strings in the app.
struct GenericWordingTests {
    static let partWords = ["prong", "prongs", "pin", "pins", "charger", "chargers", "usb", "port", "ports", "seam",
                            "seams", "plug", "plugs", "cable", "cables", "capacitor", "capacitors", "chip", "chips",
                            "wire", "wires", "connector", "connectors", "screw", "screws", "fuse", "fuses", "board",
                            "boards", "pcb", "socket", "terminal", "terminals", "motor", "hinge", "lens"]
    /// Capture-flow source folders (the report renders the backend's own words and is not checked here).
    static let folders = ["App", "Capture", "Closeups", "ObjectCapture", "Upload", "Design"]

    @Test func captureStringsNameNoPartKinds() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "RepairOS")
        let literal = try Regex(#""(?:[^"\\]|\\.)*""#)
        var hits: [String] = []
        var files = 0
        for folder in Self.folders {
            let dir = root.appending(path: folder)
            guard let walker = FileManager.default.enumerator(at: dir, includingPropertiesForKeys: nil) else { continue }
            // Preview galleries hold sample content for design review; they are not shown in the capture flow.
            for case let url as URL in walker where url.pathExtension == "swift" && !url.lastPathComponent.contains("Gallery") {
                files += 1
                let lines = try String(contentsOf: url, encoding: .utf8).split(separator: "\n", omittingEmptySubsequences: false)
                for (n, line) in lines.enumerated() {
                    let code = line.trimmingCharacters(in: .whitespaces)
                    if code.hasPrefix("//") { continue }
                    for m in code.matches(of: literal) {
                        let words = code[m.range].lowercased().split { !$0.isLetter }.map(String.init)
                        if let w = words.first(where: Self.partWords.contains) {
                            hits.append("\(folder)/\(url.lastPathComponent):\(n + 1) \"\(w)\"")
                        }
                    }
                }
            }
        }
        #expect(files > 10)
        #expect(hits.isEmpty, "object-specific words in app strings: \(hits)")
    }
}
