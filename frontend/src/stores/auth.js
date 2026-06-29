import { defineStore } from 'pinia'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('token') || '',
    username: localStorage.getItem('username') || '',
    role: localStorage.getItem('role') || '',
  }),
  actions: {
    setAuth(d) {
      this.token = d.token
      this.username = d.username
      this.role = d.role
      localStorage.setItem('token', d.token)
      localStorage.setItem('username', d.username)
      localStorage.setItem('role', d.role)
    },
    logout() {
      this.token = ''
      this.username = ''
      this.role = ''
      localStorage.removeItem('token')
      localStorage.removeItem('username')
      localStorage.removeItem('role')
    },
  },
})
