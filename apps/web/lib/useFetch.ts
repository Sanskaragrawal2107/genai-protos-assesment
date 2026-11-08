"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";

export interface FetchState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => Promise<T | null>;
  setData: (value: T | null) => void;
}

/** Small client-side data hook: GETs a path and exposes reload/loading/error. */
export function useFetch<T>(path: string | null): FetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(Boolean(path));

  const reload = useCallback(async () => {
    if (!path) {
      setLoading(false);
      return null;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await api<T>(path);
      setData(result);
      return result;
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, String(err)));
      return null;
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, error, loading, reload, setData };
}
