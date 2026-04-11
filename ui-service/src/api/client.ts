import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

export const apiClient = axios.create({
  baseURL: `${BASE_URL}/api/v1`,
  timeout: 10000,
})

apiClient.interceptors.response.use(
  (res) => res,
  (err) => {
    console.error('[API Error]', err.message)
    return Promise.reject(err)
  }
)

export const SSE_URL = `${BASE_URL}/api/v1/decision-log/stream`
