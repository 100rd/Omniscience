// Package publisher emits operator events to NATS JetStream.
//
// The publisher is intentionally narrow: a single Publish method that takes
// a pre-built Event, serialises it to JSON, and publishes synchronously
// with ack confirmation. JetStream provides at-least-once and DLQ routing —
// see ADR-0007 §Ingestion.
package publisher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/100rd/Omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
)

// Publisher is the operator's outbound interface to NATS JetStream.
type Publisher interface {
	Publish(ctx context.Context, ev *entity.Event) error
	Close() error
}

// natsPublisher is the production implementation backed by a real NATS
// connection. The connection and JetStream context are owned by the
// publisher and closed via Close().
type natsPublisher struct {
	conn          *nats.Conn
	js            jetstream.JetStream
	subjectPrefix string
}

// New connects to NATS at url, optionally authenticating via a credentials
// file path, and returns a Publisher. The connection is established
// synchronously; New returns an error if the broker is unreachable.
//
// credsFile may be empty in development. In every non-dev environment the
// Helm chart wires it to a Secret-backed file path — see ADR-0007 §ACL.
func New(url, credsFile, subjectPrefix string) (Publisher, error) {
	if url == "" {
		return nil, errors.New("publisher: nats url is required")
	}
	opts := []nats.Option{
		nats.Name("omniscience-operator"),
		nats.MaxReconnects(-1), // unbounded reconnect; the operator runs forever
	}
	if credsFile != "" {
		opts = append(opts, nats.UserCredentials(credsFile))
	}
	conn, err := nats.Connect(url, opts...)
	if err != nil {
		return nil, fmt.Errorf("publisher: nats connect: %w", err)
	}
	js, err := jetstream.New(conn)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("publisher: jetstream context: %w", err)
	}
	return &natsPublisher{
		conn:          conn,
		js:            js,
		subjectPrefix: subjectPrefix,
	}, nil
}

// Publish marshals the event to JSON and publishes synchronously to
// "{subjectPrefix}.{workspace_id}". Returns the publish error verbatim so
// the caller (controller) can NAK / requeue on failure.
//
// Issue #165: every result branch updates the operator metrics surface. The
// controller-side surface (events_emitted_total, event_emit_duration_seconds,
// last_publish_unix_seconds) and the publisher-side surface
// (publisher_errors_total, publisher_inflight) are both updated here so the
// metrics call-sites are localised — no controller-by-controller plumbing is
// needed.
func (p *natsPublisher) Publish(ctx context.Context, ev *entity.Event) error {
	if ev == nil {
		return errors.New("publisher: event must not be nil")
	}
	// Defence-in-depth: catch a missing workspace at the publisher boundary
	// even though entity.PodToEvent never produces one.
	if ev.WorkspaceID.String() == "00000000-0000-0000-0000-000000000000" {
		return errors.New("publisher: refusing to publish event with zero workspace_id")
	}
	// Resolve the metric labels from the event. ev.Metadata["kind"] is the
	// Kubernetes kind (Pod, Deployment, etc.) and ev.Action is the
	// created/updated/deleted enum. Both are bounded — see the cardinality
	// matrix in operator/internal/metrics/metrics.go.
	kind := ev.Metadata["kind"]
	if kind == "" {
		// Defensive: an event without a kind is a controller bug. Coding
		// it as "unknown" preserves the metric's bounded cardinality
		// (one extra series, not unbounded) and surfaces the bug on the
		// dashboard.
		kind = "unknown"
	}
	action := string(ev.Action)

	body, err := json.Marshal(ev)
	if err != nil {
		// Marshal failures are coded as publish_error result + publish
		// error_type — there is no separate "marshal" classification in
		// the closed enum, and a marshal failure on a typed entity is
		// indistinguishable from a publish failure to the operator.
		opmetrics.RecordPublishError(kind, action, opmetrics.PublisherErrorPublish)
		return fmt.Errorf("publisher: marshal event: %w", err)
	}
	subject := p.subjectPrefix + "." + ev.WorkspaceID.String()

	// Inflight bump for the duration of the publish call. With JetStream's
	// synchronous Publish this gauge is 0 or 1 per kind; the gauge exists
	// so a future async batch publisher surfaces backlog without an API
	// change.
	opmetrics.PublisherInflight.WithLabelValues(kind).Inc()
	start := time.Now()
	_, perr := p.js.Publish(ctx, subject, body)
	dur := time.Since(start)
	opmetrics.PublisherInflight.WithLabelValues(kind).Dec()

	if perr != nil {
		opmetrics.RecordPublishError(kind, action, classifyNATSError(perr))
		return fmt.Errorf("publisher: jetstream publish %s: %w", subject, perr)
	}

	opmetrics.RecordPublishSuccess(kind, action, dur)
	return nil
}

// classifyNATSError maps a NATS / JetStream error to the closed error_type
// enum used by the publisher_errors_total metric. The classification is a
// best-effort string match against the upstream nats.go errors — the package
// does not export typed errors for every case, so substring matching on the
// error message is the contract surface.
//
// Unknown errors fall through to PublisherErrorPublish so the metric is never
// "unlabelled" — silent buckets are worse than over-broad ones for alerting.
func classifyNATSError(err error) string {
	if err == nil {
		return opmetrics.PublisherErrorPublish
	}
	msg := err.Error()
	switch {
	case strings.Contains(msg, "no responders"):
		return opmetrics.PublisherErrorNATSNoResponders
	case strings.Contains(msg, "timeout"):
		// JetStream's ack-timeout error wraps "timeout" in its message;
		// treat any timeout as ack_timeout for alerting purposes.
		return opmetrics.PublisherErrorAckTimeout
	case strings.Contains(msg, "connection") || strings.Contains(msg, "connect"):
		return opmetrics.PublisherErrorConnect
	default:
		return opmetrics.PublisherErrorPublish
	}
}

// Close drains the underlying NATS connection. Safe to call multiple times.
func (p *natsPublisher) Close() error {
	if p.conn != nil && !p.conn.IsClosed() {
		return p.conn.Drain()
	}
	return nil
}
