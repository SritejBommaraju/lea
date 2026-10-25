#if DEBUG
import SwiftUI

// The recreation on its own dark stage for simulator screenshots (Design/DesignGallery `--ui-screen recreation`):
//   --ui-recreation <folder with recreation.json>   a phone shell (Tier 1) or a fetched gateway one (Tier 2)
//   --ui-markers <json>                              [{"id", "number", "point": [x, y, z], "tone"}] (scan frame)
//   --ui-focus                                       start focused on the first marker
struct RecreationGalleryScreen: View {
    @State private var focus: String?

    var body: some View {
        let folder = Self.value("--ui-recreation").map { URL(fileURLWithPath: $0, isDirectory: true) }
        let pins = Self.markers()
        ZStack(alignment: .top) {
            LinearGradient(colors: [Color(white: 0.09), Color(white: 0.02)], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
            VStack(spacing: 12) {
                Text("Your scan").font(.headline).foregroundStyle(.white.opacity(0.9)).padding(.top, 8)
                RecreationView(source: folder.map { .folder($0) } ?? .none, markers: pins, focus: focus) { focus = $0 }
                    .frame(maxWidth: .infinity)
                    .frame(height: 440)
                HStack(spacing: 10) {
                    ForEach(pins.filter { $0.number != nil }) { p in
                        Button("\(p.number ?? 0)") { focus = focus == p.id ? nil : p.id }
                            .buttonStyle(.borderedProminent).tint(p.tone.color)
                    }
                }
                Spacer()
            }
        }
        .preferredColorScheme(.dark)
        .onAppear { if CommandLine.arguments.contains("--ui-focus") { focus = pins.first?.id } }
    }

    static func markers() -> [ModelPin] {
        guard let path = value("--ui-markers"), let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return list.compactMap { m in
            guard let id = m["id"] as? String, let p = (m["point"] as? [NSNumber])?.map(\.floatValue), p.count == 3 else { return nil }
            let tone: Tone = (m["tone"] as? String) == "danger" ? .danger : .caution
            return ModelPin(id: id, number: m["number"] as? Int, point: SIMD3(p[0], p[1], p[2]), tone: tone)
        }
    }

    private static func value(_ flag: String) -> String? {
        let a = CommandLine.arguments
        return a.firstIndex(of: flag).flatMap { $0 + 1 < a.count ? a[$0 + 1] : nil }
    }
}
#endif
