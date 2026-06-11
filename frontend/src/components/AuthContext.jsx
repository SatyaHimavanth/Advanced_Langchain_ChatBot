import { createContext, useContext, useState, useEffect, useCallback } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

const AuthContext = createContext();

export function AuthProvider({ children }) {
  const [token, setToken] = useState(localStorage.getItem('token'));
  const [refreshToken, setRefreshToken] = useState(localStorage.getItem('refresh_token'));
  const [isAuthenticated, setIsAuthenticated] = useState(!!token);

  useEffect(() => {
    if (token) {
      localStorage.setItem('token', token);
      setIsAuthenticated(true);
    } else {
      localStorage.removeItem('token');
      setIsAuthenticated(false);
    }
  }, [token]);

  useEffect(() => {
    if (refreshToken) {
      localStorage.setItem('refresh_token', refreshToken);
    } else {
      localStorage.removeItem('refresh_token');
    }
  }, [refreshToken]);

  const login = async (username, password) => {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Login failed');
    }
    const data = await res.json();
    setToken(data.access_token);
    setRefreshToken(data.refresh_token);
  };

  const register = async (username, password) => {
    const res = await fetch(`${API_BASE}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || 'Registration failed');
    }
    const data = await res.json();
    setToken(data.access_token);
    setRefreshToken(data.refresh_token);
  };

  const logout = useCallback(() => {
    setToken(null);
    setRefreshToken(null);
  }, []);

  const refreshTokens = useCallback(async () => {
    if (!refreshToken) throw new Error("No refresh token");
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      logout();
      throw new Error("Refresh token expired");
    }
    const data = await res.json();
    setToken(data.access_token);
    setRefreshToken(data.refresh_token);
    return data.access_token;
  }, [refreshToken, logout]);

  // Wrapper for API calls to auto-refresh token
  const apiFetch = useCallback(async (endpoint, options = {}) => {
    let headers = {
      ...options.headers,
      Authorization: `Bearer ${token}`
    };

    let res = await fetch(`${API_BASE}${endpoint}`, { ...options, headers });

    if (res.status === 401 && refreshToken) {
      try {
        const newToken = await refreshTokens();
        headers = {
          ...options.headers,
          Authorization: `Bearer ${newToken}`
        };
        res = await fetch(`${API_BASE}${endpoint}`, { ...options, headers });
      } catch (err) {
        // Refresh failed, user is logged out
        return res; // let calling component handle the failure
      }
    }
    return res;
  }, [token, refreshToken, refreshTokens]);

  return (
    <AuthContext.Provider value={{ isAuthenticated, token, login, register, logout, apiFetch }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
