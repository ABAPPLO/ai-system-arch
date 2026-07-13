import { create } from 'zustand';
import { getAuth, clearAuth, AuthState } from './api/client';

interface PortalStore {
  auth: AuthState | null;
  logout: () => void;
  refresh: () => void;
}

export const useStore = create<PortalStore>((set) => ({
  auth: getAuth(),
  logout: () => {
    clearAuth();
    set({ auth: null });
  },
  refresh: () => set({ auth: getAuth() }),
}));
