// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useState } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/badge";
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell,
} from "@/components/ui/table";
import { Spinner } from "@/components/ui/spinner";
import { api } from "../api";
import type { WorkflowIssue } from "../api";
import { useTranslation } from "react-i18next";

type IssueList =
  | { status: "loading" | "error" }
  | { status: "ready"; issues: WorkflowIssue[] };

export default function Errors() {
  const { t } = useTranslation();
  const [result, setResult] = useState<IssueList>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    api.getWorkflowIssues(controller.signal).then(issues => {
      if (!controller.signal.aborted) setResult({ status: "ready", issues });
    }).catch(() => {
      if (!controller.signal.aborted) setResult({ status: "error" });
    });
    return () => controller.abort();
  }, []);

  return (
    <div className="animate-in space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{t("errors.ui.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">{t("errors.ui.subtitle")}</p>
      </div>

      {result.status === "loading" && (
        <div className="flex items-center justify-center py-20">
          <Spinner size="lg" />
        </div>
      )}

      {result.status === "error" && (
        <p role="alert" className="rounded-md border border-destructive/30 p-4 text-sm text-destructive">
          {t("errors.ui.loadFailed")}
        </p>
      )}

      {result.status === "ready" && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wide">{t("errors.ui.unresolvedErrors")}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-bold">{result.issues.length}</div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-xs font-medium text-muted-foreground uppercase tracking-wide">{t("errors.ui.humanActionNeeded")}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-bold">{result.issues.filter(issue => issue.status === "PO_REVIEW").length}</div>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("errors.ui.errorLog")}</CardTitle>
            </CardHeader>
            <CardContent>
              {result.issues.length === 0 ? (
                <p className="text-center text-sm text-muted-foreground py-8">{t("errors.ui.noErrors")}</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("errors.ui.colTime")}</TableHead>
                      <TableHead>{t("errors.ui.colDocument")}</TableHead>
                      <TableHead>{t("errors.ui.colStatus")}</TableHead>
                      <TableHead>{t("errors.ui.colMessage")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {result.issues.map(issue => {
                      const needsReview = issue.status === "PO_REVIEW";
                      return (
                        <TableRow key={issue.document_id}>
                          <TableCell className="text-xs whitespace-nowrap">
                            {issue.updated_at ? new Date(issue.updated_at).toLocaleString() : "—"}
                          </TableCell>
                          <TableCell>{issue.document_id}</TableCell>
                          <TableCell>
                            <StatusBadge status={needsReview ? "warning" : "failed"}>
                              {t(needsReview ? "errors.ui.poReview" : "errors.ui.failed")}
                            </StatusBadge>
                          </TableCell>
                          <TableCell className="whitespace-normal break-words">{issue.error || "—"}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
