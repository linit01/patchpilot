import SwiftUI

/// Settings tab: profile, server config, schedules, about
struct SettingsView: View {
    @EnvironmentObject var authService: AuthService
    @State private var showChangePassword = false
    @State private var showServerConfig = false

    var body: some View {
        NavigationStack {
            List {
                // User Profile Section
                Section {
                    if let user = authService.currentUser {
                        HStack(spacing: 12) {
                            Image(systemName: "person.circle.fill")
                                .font(.largeTitle)
                                .foregroundColor(Theme.cyan)

                            VStack(alignment: .leading, spacing: 4) {
                                Text(user.username)
                                    .font(.headline)
                                    .foregroundColor(Theme.textPrimary)
                                Text(user.role.displayName)
                                    .font(.caption)
                                    .foregroundColor(Theme.textSecondary)
                            }
                        }
                        .listRowBackground(Theme.bgPanel)
                    }

                    Button {
                        showChangePassword = true
                    } label: {
                        Label("Change Password", systemImage: "lock.rotation")
                            .foregroundColor(Theme.textPrimary)
                    }
                    .listRowBackground(Theme.bgPanel)
                } header: {
                    Text("Account")
                        .foregroundColor(Theme.textMuted)
                }

                // Schedules Section (admin+ only)
                if authService.currentUser?.role.canWrite == true {
                    Section {
                        NavigationLink {
                            ScheduleListView()
                                .navigationTitle("Schedules")
                        } label: {
                            Label("Patch Schedules", systemImage: "calendar.badge.clock")
                                .foregroundColor(Theme.textPrimary)
                        }
                        .listRowBackground(Theme.bgPanel)
                    } header: {
                        Text("Management")
                            .foregroundColor(Theme.textMuted)
                    }
                }

                // Server Section
                Section {
                    if let serverURL = KeychainHelper.serverURL {
                        HStack {
                            Text("Server")
                                .foregroundColor(Theme.textSecondary)
                            Spacer()
                            Text(serverURL)
                                .font(.caption)
                                .foregroundColor(Theme.textMuted)
                                .lineLimit(1)
                        }
                        .listRowBackground(Theme.bgPanel)
                    }

                    Button {
                        showServerConfig = true
                    } label: {
                        Label("Change Server", systemImage: "server.rack")
                            .foregroundColor(Theme.textPrimary)
                    }
                    .listRowBackground(Theme.bgPanel)
                } header: {
                    Text("Connection")
                        .foregroundColor(Theme.textMuted)
                }

                // Logout
                Section {
                    Button(role: .destructive) {
                        authService.logout()
                    } label: {
                        Label("Sign Out", systemImage: "rectangle.portrait.and.arrow.right")
                            .foregroundColor(Theme.red)
                    }
                    .listRowBackground(Theme.bgPanel)
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(Theme.bgBlack)
            .navigationTitle("Settings")
            .sheet(isPresented: $showChangePassword) {
                ChangePasswordSheet()
            }
            .sheet(isPresented: $showServerConfig) {
                ServerConfigView()
            }
        }
    }
}

/// Change password sheet
struct ChangePasswordSheet: View {
    @EnvironmentObject var authService: AuthService
    @Environment(\.dismiss) private var dismiss
    @State private var currentPassword = ""
    @State private var newPassword = ""
    @State private var confirmPassword = ""
    @State private var errorMessage: String?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Current Password", text: $currentPassword)
                    SecureField("New Password", text: $newPassword)
                    SecureField("Confirm New Password", text: $confirmPassword)
                }

                if let error = errorMessage {
                    Text(error)
                        .foregroundColor(Theme.red)
                        .font(.caption)
                }

                Button(action: changePassword) {
                    HStack {
                        if isLoading { ProgressView() }
                        Text("Change Password")
                    }
                }
                .disabled(!canSubmit)
            }
            .navigationTitle("Change Password")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium])
    }

    private var canSubmit: Bool {
        !currentPassword.isEmpty && !newPassword.isEmpty &&
        newPassword == confirmPassword && !isLoading
    }

    private func changePassword() {
        guard newPassword == confirmPassword else {
            errorMessage = "Passwords don't match"
            return
        }
        isLoading = true
        errorMessage = nil
        Task {
            do {
                try await authService.changePassword(
                    currentPassword: currentPassword,
                    newPassword: newPassword
                )
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
            }
            isLoading = false
        }
    }
}
