import SwiftUI

/// Bottom sheet for confirming patch operation with sudo password
struct PatchConfirmSheet: View {
    let hostnames: [String]
    let onDismiss: () -> Void

    @StateObject private var hostService = HostService()
    @StateObject private var wsService = WebSocketService()
    @State private var sudoPassword = ""
    @State private var isPatching = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if isPatching || wsService.isConnected {
                    patchProgressView
                } else {
                    patchConfirmView
                }
            }
            .padding()
            .background(Theme.bgBlack)
            .navigationTitle("Patch Hosts")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { onDismiss() }
                        .foregroundColor(Theme.textSecondary)
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: - Confirm View

    private var patchConfirmView: some View {
        VStack(spacing: 16) {
            Image(systemName: "arrow.down.circle.fill")
                .font(.system(size: 40))
                .foregroundColor(Theme.green)

            Text("Patch \(hostnames.count) host\(hostnames.count == 1 ? "" : "s")?")
                .font(.headline)
                .foregroundColor(Theme.textPrimary)

            VStack(alignment: .leading, spacing: 4) {
                ForEach(hostnames, id: \.self) { name in
                    Label(name, systemImage: "desktopcomputer")
                        .font(.subheadline)
                        .foregroundColor(Theme.textSecondary)
                }
            }

            SecureField("Sudo password (optional)", text: $sudoPassword)
                .textFieldStyle(.roundedBorder)

            if let error = errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundColor(Theme.red)
            }

            Button(action: startPatch) {
                Text("Start Patching")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(Theme.green)
                    .foregroundColor(.white)
                    .cornerRadius(10)
            }
        }
    }

    // MARK: - Progress View

    private var patchProgressView: some View {
        VStack(spacing: 12) {
            HStack {
                if wsService.patchComplete {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(Theme.green)
                    Text("Patching Complete")
                        .foregroundColor(Theme.green)
                } else {
                    ProgressView()
                    Text("Patching in progress...")
                        .foregroundColor(Theme.amber)
                }
                Spacer()
            }
            .font(.headline)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(wsService.messages.enumerated()), id: \.offset) { index, msg in
                            Text(msg)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(Theme.textSecondary)
                                .id(index)
                        }
                    }
                }
                .onChange(of: wsService.messages.count) { _, newCount in
                    if newCount > 0 {
                        proxy.scrollTo(newCount - 1, anchor: .bottom)
                    }
                }
            }
            .frame(maxHeight: .infinity)
            .padding(8)
            .background(Theme.bgCardInner)
            .cornerRadius(8)

            if wsService.patchComplete {
                Button("Done") { onDismiss() }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.cyan)
            }
        }
    }

    // MARK: - Actions

    private func startPatch() {
        isPatching = true
        errorMessage = nil

        // Connect WebSocket for real-time output
        wsService.connect()

        Task {
            do {
                try await hostService.patchHosts(
                    hostnames: hostnames,
                    becomePassword: sudoPassword.isEmpty ? nil : sudoPassword
                )
            } catch {
                errorMessage = error.localizedDescription
                isPatching = false
            }
        }
    }
}
