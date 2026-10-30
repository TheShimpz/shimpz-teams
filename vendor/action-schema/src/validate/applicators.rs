//! The keywords that apply subschemas or search patterns, each yielding errors as `jsonschema` 4.25.1 yields them.

use std::collections::HashSet;
use std::ops::ControlFlow;

use strict_json::Value;

use super::{Evaluator, GO_ON, PayloadError, Sink, Step};
use crate::numbers;
use crate::walk::{elements, members, reference_target};

impl<'a> Evaluator<'a, '_> {
    pub(super) fn reference(
        &mut self,
        reference: &Value,
        instance: &Value,
        sink: Sink<'_>,
    ) -> Step {
        let Value::String(reference) = reference else {
            return Err(PayloadError::Unsupported);
        };
        let target = reference_target(self.root, reference).ok_or(PayloadError::Unsupported)?;
        self.descend(instance, target, sink)
    }

    pub(super) fn all_of(&mut self, list: &'a Value, instance: &Value, sink: Sink<'_>) -> Step {
        for subschema in elements(list) {
            if self.descend(instance, subschema, sink)?.is_break() {
                return Ok(ControlFlow::Break(()));
            }
        }
        GO_ON
    }

    /// Evaluates each subschema completely until one has no error; none passing is one error.
    pub(super) fn any_of(&mut self, list: &'a Value, instance: &Value, sink: Sink<'_>) -> Step {
        for subschema in elements(list) {
            if !self.has_errors(instance, subschema)? {
                return GO_ON;
            }
        }
        Ok(sink())
    }

    /// Evaluates each subschema completely until one has no error, then probes every later one; none passing, or a
    /// later one passing too, is one error.
    pub(super) fn one_of(&mut self, list: &'a Value, instance: &Value, sink: Sink<'_>) -> Step {
        let subschemas = elements(list);
        let Some(first) = self.first_passing(subschemas, instance)? else {
            return Ok(sink());
        };
        let mut more = false;
        for subschema in &subschemas[first + 1..] {
            more |= self.is_valid(instance, subschema)?;
        }
        if more { Ok(sink()) } else { GO_ON }
    }

    fn first_passing(
        &mut self,
        subschemas: &'a [Value],
        instance: &Value,
    ) -> Result<Option<usize>, PayloadError> {
        for (index, subschema) in subschemas.iter().enumerate() {
            if !self.has_errors(instance, subschema)? {
                return Ok(Some(index));
            }
        }
        Ok(None)
    }

    pub(super) fn not(&mut self, subschema: &'a Value, instance: &Value, sink: Sink<'_>) -> Step {
        if self.is_valid(instance, subschema)? {
            Ok(sink())
        } else {
            GO_ON
        }
    }

    pub(super) fn if_then_else(
        &mut self,
        condition: &'a Value,
        instance: &Value,
        schema: &'a Value,
        sink: Sink<'_>,
    ) -> Step {
        let branch = if self.is_valid(instance, condition)? {
            schema.get("then")
        } else {
            schema.get("else")
        };
        match branch {
            Some(branch) => self.descend(instance, branch, sink),
            None => GO_ON,
        }
    }

    pub(super) fn properties(&mut self, map: &'a Value, instance: &Value, sink: Sink<'_>) -> Step {
        if !matches!(instance, Value::Object(_)) {
            return GO_ON;
        }
        for (name, subschema) in members(map) {
            if let Some(value) = instance.get(name)
                && self.descend(value, subschema, sink)?.is_break()
            {
                return Ok(ControlFlow::Break(()));
            }
        }
        GO_ON
    }

    pub(super) fn pattern(&mut self, pattern: &Value, instance: &Value, sink: Sink<'_>) -> Step {
        match (pattern, instance) {
            (Value::String(pattern), Value::String(subject))
                if !self.matches(pattern, subject)? =>
            {
                Ok(sink())
            }
            _ => GO_ON,
        }
    }

    /// For each pattern in schema order, every member whose name it matches, in payload order.
    pub(super) fn pattern_properties(
        &mut self,
        map: &'a Value,
        instance: &Value,
        sink: Sink<'_>,
    ) -> Step {
        if !matches!(instance, Value::Object(_)) {
            return GO_ON;
        }
        for (pattern, subschema) in members(map) {
            for (name, value) in members(instance) {
                if self.matches(pattern, name)? && self.descend(value, subschema, sink)?.is_break()
                {
                    return Ok(ControlFlow::Break(()));
                }
            }
        }
        GO_ON
    }

    /// Team's `additionalProperties`: every member neither declared nor matched by a pattern, found first, in payload
    /// order, each name tried against the patterns in schema order until one matches.
    pub(super) fn additional_properties(
        &mut self,
        additional: &'a Value,
        instance: &Value,
        schema: &'a Value,
        sink: Sink<'_>,
    ) -> Step {
        if !matches!(instance, Value::Object(_)) {
            return GO_ON;
        }
        let extras = self.extras(instance, schema)?;
        match additional {
            Value::Object(_) => {
                for extra in extras {
                    if self.descend(extra, additional, sink)?.is_break() {
                        return Ok(ControlFlow::Break(()));
                    }
                }
                GO_ON
            }
            Value::Bool(false) if !extras.is_empty() => Ok(sink()),
            _ => GO_ON,
        }
    }

    fn extras<'v>(
        &mut self,
        instance: &'v Value,
        schema: &Value,
    ) -> Result<Vec<&'v Value>, PayloadError> {
        let declared = schema.get("properties");
        let patterns = schema.get("patternProperties").map_or(&[][..], members);
        let mut extras = Vec::new();
        for (name, value) in members(instance) {
            if declared.and_then(|declared| declared.get(name)).is_some() {
                continue;
            }
            if !self.any_matches(patterns, name)? {
                extras.push(value);
            }
        }
        Ok(extras)
    }

    fn any_matches(
        &mut self,
        patterns: &[(String, Value)],
        name: &str,
    ) -> Result<bool, PayloadError> {
        for (pattern, _) in patterns {
            if self.matches(pattern, name)? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub(super) fn property_names(
        &mut self,
        subschema: &'a Value,
        instance: &Value,
        sink: Sink<'_>,
    ) -> Step {
        for (name, _) in members(instance) {
            if self
                .descend(&Value::String(name.clone()), subschema, sink)?
                .is_break()
            {
                return Ok(ControlFlow::Break(()));
            }
        }
        GO_ON
    }

    pub(super) fn dependent_schemas(
        &mut self,
        map: &'a Value,
        instance: &Value,
        sink: Sink<'_>,
    ) -> Step {
        if !matches!(instance, Value::Object(_)) {
            return GO_ON;
        }
        for (name, subschema) in members(map) {
            if instance.get(name).is_some() && self.descend(instance, subschema, sink)?.is_break() {
                return Ok(ControlFlow::Break(()));
            }
        }
        GO_ON
    }

    pub(super) fn prefix_items(
        &mut self,
        list: &'a Value,
        instance: &Value,
        sink: Sink<'_>,
    ) -> Step {
        let Value::Array(items) = instance else {
            return GO_ON;
        };
        for (item, subschema) in items.iter().zip(elements(list)) {
            if self.descend(item, subschema, sink)?.is_break() {
                return Ok(ControlFlow::Break(()));
            }
        }
        GO_ON
    }

    /// The items after the `prefixItems`: refused all at once by `items: false`, else each evaluated in turn.
    pub(super) fn items(
        &mut self,
        subschema: &'a Value,
        instance: &Value,
        schema: &'a Value,
        sink: Sink<'_>,
    ) -> Step {
        let Value::Array(items) = instance else {
            return GO_ON;
        };
        let prefix = schema
            .get("prefixItems")
            .map_or(0, |list| elements(list).len());
        let rest = items.get(prefix..).unwrap_or_default();
        if rest.is_empty() {
            return GO_ON;
        }
        if *subschema == Value::Bool(false) {
            return Ok(sink());
        }
        for item in rest {
            if self.descend(item, subschema, sink)?.is_break() {
                return Ok(ControlFlow::Break(()));
            }
        }
        GO_ON
    }

    /// Counts the items `contains` accepts, each probed to its first error, and reports one error once the count
    /// passes `maxContains` (default: the item count) or ends below `minContains` (default 1).
    pub(super) fn contains(
        &mut self,
        subschema: &'a Value,
        instance: &Value,
        schema: &'a Value,
        sink: Sink<'_>,
    ) -> Step {
        let Value::Array(items) = instance else {
            return GO_ON;
        };
        let mut matched = 0_usize;
        for item in items {
            if self.is_valid(item, subschema)? {
                matched += 1;
                if exceeds(matched, schema.get("maxContains"), items.len()) {
                    return Ok(sink());
                }
            }
        }
        if below(matched, schema.get("minContains")) {
            Ok(sink())
        } else {
            GO_ON
        }
    }

    /// `unevaluatedItems`: one error when an item is neither evaluated by an applicator that succeeded nor accepted
    /// by the `contains` or `unevaluatedItems` subschema.
    pub(super) fn unevaluated_items(
        &mut self,
        instance: &Value,
        schema: &'a Value,
        sink: Sink<'_>,
    ) -> Step {
        let Value::Array(items) = instance else {
            return GO_ON;
        };
        let mut evaluated = HashSet::new();
        self.evaluated_items(instance, schema, &mut evaluated)?;
        if (0..items.len()).all(|index| evaluated.contains(&index)) {
            GO_ON
        } else {
            Ok(sink())
        }
    }

    /// `jsonschema`'s `find_evaluated_item_indexes_by_schema`, probing in its order.
    fn evaluated_items(
        &mut self,
        instance: &Value,
        schema: &'a Value,
        evaluated: &mut HashSet<usize>,
    ) -> Result<(), PayloadError> {
        let items = elements(instance);
        if !matches!(schema, Value::Object(_)) {
            return Ok(());
        }
        if schema.get("items").is_some() {
            evaluated.extend(0..items.len());
            return Ok(());
        }
        if let Some(Value::String(reference)) = schema.get("$ref") {
            let target = reference_target(self.root, reference).ok_or(PayloadError::Unsupported)?;
            self.evaluated_items(instance, target, evaluated)?;
        }
        if let Some(prefix) = schema.get("prefixItems") {
            evaluated.extend(0..elements(prefix).len());
        }
        self.evaluated_by_condition(instance, schema, evaluated)?;
        self.evaluated_by_items(items, schema, evaluated)?;
        for keyword in ["allOf", "oneOf", "anyOf"] {
            for subschema in schema.get(keyword).map_or(&[][..], elements) {
                if self.is_valid(instance, subschema)? {
                    self.evaluated_items(instance, subschema, evaluated)?;
                }
            }
        }
        Ok(())
    }

    /// The items the `contains` or `unevaluatedItems` subschema accepts, each probed to its first error.
    fn evaluated_by_items(
        &mut self,
        items: &[Value],
        schema: &'a Value,
        evaluated: &mut HashSet<usize>,
    ) -> Result<(), PayloadError> {
        for keyword in ["contains", "unevaluatedItems"] {
            let Some(subschema) = schema.get(keyword) else {
                continue;
            };
            for (index, item) in items.iter().enumerate() {
                if self.is_valid(item, subschema)? {
                    evaluated.insert(index);
                }
            }
        }
        Ok(())
    }

    fn evaluated_by_condition(
        &mut self,
        instance: &Value,
        schema: &'a Value,
        evaluated: &mut HashSet<usize>,
    ) -> Result<(), PayloadError> {
        let Some(condition) = schema.get("if") else {
            return Ok(());
        };
        if self.is_valid(instance, condition)? {
            self.evaluated_items(instance, condition, evaluated)?;
            if let Some(then) = schema.get("then") {
                self.evaluated_items(instance, then, evaluated)?;
            }
        } else if let Some(otherwise) = schema.get("else") {
            self.evaluated_items(instance, otherwise, evaluated)?;
        }
        Ok(())
    }
}

/// Whether `count` passes a `maxContains` bound, or the item count when there is none.
fn exceeds(count: usize, bound: Option<&Value>, items: usize) -> bool {
    match bound {
        Some(Value::Number(bound)) => numbers::compare_count(count, bound).is_gt(),
        _ => count > items,
    }
}

/// Whether `count` is below a `minContains` bound, or 1 when there is none.
fn below(count: usize, bound: Option<&Value>) -> bool {
    match bound {
        Some(Value::Number(bound)) => numbers::compare_count(count, bound).is_lt(),
        _ => count < 1,
    }
}
