import SwiftUI
import WebKit

/// VEL's whiteboard (its stage renderer, bundled as vel-stage.js) in a web view: each step is drawn while Lea says
/// its line, the way VEL teaches. The drawings come from the agent; they explain, they are not evidence.
struct WhiteboardView: View {
    @EnvironmentObject private var conversation: VoiceCoordinator
    @Environment(\.dismiss) private var dismiss
    let lesson: LeaActions.Whiteboard
    @State private var web = WKWebView()
    @State private var current = -1

    var body: some View {
        NavigationStack {
            BoardWeb(web: web)
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle(lesson.title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                    ToolbarItem(placement: .primaryAction) { Button("Replay") { Task { await play() } } }
                }
                .safeAreaInset(edge: .bottom) {
                    if lesson.steps.indices.contains(current) {
                        Text(lesson.steps[current].say).font(.callout).padding().frame(maxWidth: .infinity).background(.thinMaterial)
                    }
                }
        }
        .task { await play() }
    }

    private func play() async {
        guard await BoardWeb.ready(web) else { return }
        let json = String(data: (try? JSONEncoder().encode(lesson.steps)) ?? Data("[]".utf8), encoding: .utf8) ?? "[]"
        _ = try? await web.evaluateJavaScript("play(\(json))")
        for (i, step) in lesson.steps.enumerated() {
            guard !Task.isCancelled else { return }
            current = i
            let drawMs = (try? await web.evaluateJavaScript("step(\(i))")) as? Double ?? 0
            let started = Date()
            await conversation.narrate(step.say)
            let left = drawMs / 1000 - Date().timeIntervalSince(started)
            if left > 0 { try? await Task.sleep(for: .seconds(left)) }
        }
    }
}

private struct BoardWeb: UIViewRepresentable {
    let web: WKWebView

    func makeUIView(context: Context) -> WKWebView {
        web.isOpaque = false
        web.scrollView.isScrollEnabled = false
        if let page = Bundle.main.url(forResource: "whiteboard", withExtension: "html") {
            web.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        }
        return web
    }

    func updateUIView(_ view: WKWebView, context: Context) {}

    /// Waits (up to 5 s) until the page's `play` function exists.
    static func ready(_ web: WKWebView) async -> Bool {
        for _ in 0..<50 {
            if (try? await web.evaluateJavaScript("typeof play === 'function' && typeof VelStage === 'function'")) as? Bool == true { return true }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return false
    }
}
