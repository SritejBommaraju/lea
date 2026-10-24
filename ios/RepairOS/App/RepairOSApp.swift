import SwiftUI

@main
struct RepairOSApp: App {
    @State private var app = AppState()

    var body: some Scene {
        WindowGroup {
            root
                .environment(app)
                .task { await app.launch() }
        }
    }

    @ViewBuilder private var root: some View {
        #if DEBUG
        if let screen = UIGallery.screen { UIGallery.root(screen, app: app) } else { RootView() }
        #else
        RootView()
        #endif
    }
}
